"""Konsolidierte Auswertung: Gruppen, Eliminierungen, manuelle Buchungen, Schnittstellen."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, create_session_factory
from app.services.audit_export import build_audit_export_package
from app.services.chat import chat_tool_blocked, chat_tool_requires_confirmation
from app.services.consolidation import (
    CARRY_CODE,
    DIFFERENCE_DEBT_CODE,
    DIFFERENCE_INCOME_CODE,
    ConsolidationError,
    consolidation_report,
    consolidation_report_csv,
    create_adjustment,
    create_group,
    delete_adjustment,
    json_ready,
    remove_member,
    set_member,
    update_group,
)
from app.services.internal_api import InProcessApiClient
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.mcp_server import MCPServer
from app.services.partners import PartnerError, create_partner, update_partner
from app.services.periods import close_fiscal_year
from app.services.reports import (
    balance_sheet_for_company,
    income_statement_for_company,
    trial_balance_for_company,
)
from domain.models import (
    Account,
    AuditLog,
    Base,
    BusinessPartner,
    Company,
    ConsolidationGroup,
    FiscalYear,
    JournalEntry,
    Tenant,
    User,
)

ZERO = Decimal("0.00")
ACCOUNTS = (
    ("1200", "Bank", "asset", None),
    ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
    ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", "creditor"),
    ("0860", "Gewinnvortrag vor Verwendung", "equity", None),
    ("0800", "Gezeichnetes Kapital", "equity", None),
    ("0500", "Beteiligungen", "asset", None),
    ("8400", "Erlöse", "income", None),
    ("4900", "Sonstige Aufwendungen", "expense", None),
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _tenant(session: Session, name: str = "Gruppe") -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    session.flush()
    return tenant


def _company(session: Session, tenant: Tenant, name: str, currency_code: str = "EUR") -> Company:
    company = Company(tenant=tenant, name=name, currency_code=currency_code)
    session.add(company)
    session.flush()
    for code, account_name, account_type, subledger in ACCOUNTS:
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=account_name,
                account_type=account_type,
                subledger=subledger,
            )
        )
    session.commit()
    return company


def _account(session: Session, company: Company, code: str) -> Account:
    return session.execute(
        select(Account).where(Account.company_id == company.id, Account.code == code)
    ).scalar_one()


def _partner_for(
    session: Session, owner: Company, target: Company | None, name: str | None = None
) -> BusinessPartner:
    """Partner in den Büchern von ``owner``, der auf ``target`` zeigt (Kunde und Lieferant)."""
    return create_partner(
        session=session,
        company=owner,
        changed_by="t",
        name=name or (target.name if target else "Fremdfirma"),
        is_customer=True,
        is_supplier=True,
        intercompany_company_id=target.id if target else None,
    )


def _book(
    session: Session,
    company: Company,
    lines: list[tuple[str, str, str, BusinessPartner | None]],
    *,
    entry_date: date = date(2026, 3, 1),
    closing: bool = False,
) -> JournalEntry:
    """Bucht Zeilen (Konto, Soll, Haben, Partner)."""
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Testbuchung",
            status="posted",
            changed_by="t",
            post_to_closing_period=closing,
            lines=[
                JournalLineInput(
                    account_id=_account(session, company, code).id,
                    debit_amount=Decimal(debit),
                    credit_amount=Decimal(credit),
                    partner_id=partner.id if partner else None,
                )
                for code, debit, credit, partner in lines
            ],
        ),
    )


def _sale(
    session: Session,
    seller: Company,
    partner: BusinessPartner | None,
    amount: str,
    *,
    entry_date: date = date(2026, 3, 1),
) -> JournalEntry:
    return _book(
        session,
        seller,
        [("1400", amount, "0", partner), ("8400", "0", amount, None)],
        entry_date=entry_date,
    )


def _purchase(
    session: Session,
    buyer: Company,
    partner: BusinessPartner | None,
    amount: str,
    *,
    entry_date: date = date(2026, 3, 1),
) -> JournalEntry:
    return _book(
        session,
        buyer,
        [("4900", amount, "0", None), ("1600", "0", amount, partner)],
        entry_date=entry_date,
    )


def _capital(session: Session, company: Company, amount: str) -> JournalEntry:
    return _book(
        session,
        company,
        [("1200", amount, "0", None), ("0800", "0", amount, None)],
        entry_date=date(2026, 1, 2),
    )


def _group(
    session: Session, parent: Company, *members: Company, name: str = "Gruppe"
) -> ConsolidationGroup:
    group = create_group(
        session=session,
        parent_company=parent,
        changed_by="t",
        name=name,
        members=[{"company_id": company.id} for company in members],
    )
    session.commit()
    return group


def _row(rows: list[dict], code: str) -> dict | None:
    return next((row for row in rows if row["code"] == code), None)


def _two_companies(session: Session) -> tuple[Tenant, Company, Company]:
    tenant = _tenant(session)
    parent = _company(session, tenant, "Mutter GmbH")
    return tenant, parent, _company(session, tenant, "Tochter GmbH")


# ---------------------------------------------------------------------------
# Gruppen und Mitglieder
# ---------------------------------------------------------------------------


def test_group_adds_parent_and_logs_audit(session: Session) -> None:
    _, parent, child = _two_companies(session)

    group = _group(session, parent, child)

    assert {member.company_id for member in group.members} == {parent.id, child.id}
    entry = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "consolidation_group")
    ).scalar_one()
    assert entry.action == "created"
    assert entry.company_id == parent.id
    assert entry.payload["before"] is None
    assert entry.payload["after"]["name"] == "Gruppe"


def test_members_must_share_tenant_and_currency(session: Session) -> None:
    tenant, parent, child = _two_companies(session)
    foreign = _company(session, _tenant(session, "Fremd"), "Fremde GmbH")
    dollar = _company(session, tenant, "Dollar Inc", currency_code="USD")

    with pytest.raises(ConsolidationError, match="Mandanten"):
        _group(session, parent, foreign)
    session.rollback()
    with pytest.raises(ConsolidationError, match="Währung"):
        _group(session, parent, dollar)
    session.rollback()

    group = _group(session, parent, child)
    with pytest.raises(ConsolidationError, match="Währung"):
        set_member(session=session, group=group, changed_by="t", company_id=dollar.id)
    session.rollback()
    with pytest.raises(ConsolidationError, match="Mandanten"):
        update_group(
            session=session, group=group, changed_by="t", parent_company_id=foreign.id
        )
    session.rollback()


def test_report_rechecks_currency_of_members(session: Session) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)
    child.currency_code = "USD"
    session.commit()

    with pytest.raises(ConsolidationError, match="Umrechnung"):
        consolidation_report(session=session, group=group)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"ownership_percent": "0"}, "Beteiligungsquote"),
        ({"ownership_percent": "100.01"}, "Beteiligungsquote"),
        ({"ownership_percent": "abc"}, "Beteiligungsquote"),
        ({"ownership_percent": "33.333"}, "Nachkommastellen"),
        ({"included_from": "2026-07-01", "included_to": "2026-06-30"}, "Einbezogen ab"),
        ({"included_from": "morgen"}, "Datum"),
    ],
)
def test_member_values_are_validated(session: Session, values: dict, message: str) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent)

    with pytest.raises(ConsolidationError, match=message):
        set_member(session=session, group=group, changed_by="t", company_id=child.id, **values)


def test_parent_stays_member_with_full_quota(session: Session) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)

    with pytest.raises(ConsolidationError, match="Mitglied"):
        remove_member(session=session, group=group, changed_by="t", company_id=parent.id)
    session.rollback()
    with pytest.raises(ConsolidationError, match="100"):
        set_member(
            session=session,
            group=group,
            changed_by="t",
            company_id=parent.id,
            ownership_percent="60",
        )
    session.rollback()

    assert update_group(
        session=session, group=group, changed_by="t", parent_company_id=child.id
    )
    assert group.parent_company_id == child.id
    assert remove_member(session=session, group=group, changed_by="t", company_id=parent.id)
    session.commit()
    assert [member.company_id for member in group.members] == [child.id]


def test_group_names_are_unique_per_tenant_and_noop_stays_silent(session: Session) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)

    with pytest.raises(ConsolidationError, match="bereits"):
        _group(session, child, parent)
    session.rollback()
    assert update_group(session=session, group=group, changed_by="t", name="Gruppe") is False
    assert update_group(session=session, group=group, changed_by="t", name="Neu") is True
    session.commit()
    actions = session.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "consolidation_group")
    ).scalars()
    assert list(actions) == ["created", "updated"]


# ---------------------------------------------------------------------------
# Partner-Kennzeichen
# ---------------------------------------------------------------------------


def test_intercompany_partner_validation_and_history(session: Session) -> None:
    tenant, parent, child = _two_companies(session)
    foreign = _company(session, _tenant(session, "Fremd"), "Fremde GmbH")

    partner = _partner_for(session, parent, None)
    session.commit()
    assert partner.intercompany_company_id is None

    with pytest.raises(PartnerError, match="eigene Gesellschaft"):
        update_partner(
            session=session, partner=partner, changed_by="t", intercompany_company_id=parent.id
        )
    session.rollback()
    with pytest.raises(PartnerError, match="Mandanten"):
        update_partner(
            session=session, partner=partner, changed_by="t", intercompany_company_id=foreign.id
        )
    session.rollback()
    with pytest.raises(PartnerError, match="Gesellschafts-ID"):
        update_partner(
            session=session, partner=partner, changed_by="t", intercompany_company_id="abc"
        )
    session.rollback()

    assert update_partner(
        session=session, partner=partner, changed_by="t", intercompany_company_id=child.id
    )
    session.commit()
    entry = session.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == "business_partner", AuditLog.action == "updated")
        .order_by(AuditLog.id.desc())
    ).scalars().first()
    assert entry.payload["before"]["intercompany_company_id"] is None
    assert entry.payload["after"]["intercompany_company_id"] == child.id

    assert update_partner(
        session=session, partner=partner, changed_by="t", intercompany_company_id=""
    )
    assert partner.intercompany_company_id is None
    with pytest.raises(PartnerError, match="eigene Gesellschaft"):
        create_partner(
            session=session,
            company=parent,
            changed_by="t",
            name="Selbst",
            is_customer=True,
            intercompany_company_id=parent.id,
        )


# ---------------------------------------------------------------------------
# Summenbilanz und Eliminierungen
# ---------------------------------------------------------------------------


def test_columns_match_the_single_reports_without_intercompany(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, parent, "1000")
    _capital(session, child, "500")
    _sale(session, parent, _partner_for(session, parent, None), "300")
    _purchase(session, child, _partner_for(session, child, None), "120")
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    sheet = report["balance_sheet"]
    assert sheet["totals"]["is_balanced"] is True
    assert sheet["totals"]["total_assets"] == Decimal("1800.00")
    assert sheet["totals"]["intercompany_difference"] == ZERO
    for company in (parent, child):
        single = balance_sheet_for_company(
            session=session, company_id=company.id, date_to=date(2026, 12, 31)
        )
        assert sheet["summary"]["assets"]["amounts"][company.id] == single["totals"]["total_assets"]
        assert (
            sheet["summary"]["liabilities_and_equity"]["amounts"][company.id]
            == single["totals"]["total_liabilities_and_equity"]
        )
        income = income_statement_for_company(
            session=session, company_id=company.id, date_to=date(2026, 12, 31)
        )
        assert (
            report["income_statement"]["summary"]["net_income"]["amounts"][company.id]
            == income["totals"]["net_income"]
        )
        for row in trial_balance_for_company(
            session=session, company_id=company.id, date_to=date(2026, 12, 31)
        ):
            folded = _row(report["trial_balance"]["rows"], row["code"])
            assert folded["amounts"][company.id] == row["balance"]
    assert report["income_statement"]["totals"]["net_income"] == Decimal("180.00")
    assert report["eliminations"]["debt"]["pairs"] == []
    assert report["trial_balance"]["totals"]["is_balanced"] is True


def test_matching_balances_are_eliminated_on_two_companies(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, parent, "1000")
    _sale(session, parent, _partner_for(session, parent, child), "119")
    _purchase(session, child, _partner_for(session, child, parent), "119")
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    sheet = report["balance_sheet"]
    receivable = _row(sheet["assets"], "1400")
    assert receivable["total"] == Decimal("119.00")
    assert receivable["eliminations"] == Decimal("-119.00")
    assert receivable["amount"] == ZERO
    payable = _row(sheet["liabilities_and_equity"], "1600")
    assert payable["total"] == Decimal("119.00")
    assert payable["amount"] == ZERO
    assert sheet["totals"]["total_assets"] == Decimal("1000.00")
    assert sheet["totals"]["is_balanced"] is True
    assert sheet["totals"]["intercompany_difference"] == ZERO
    assert _row(sheet["assets"], DIFFERENCE_DEBT_CODE) is None

    pair = report["eliminations"]["debt"]["pairs"][0]
    assert pair["is_matched"] is True
    assert (pair["balance_a"], pair["balance_b"]) == (Decimal("119.00"), Decimal("-119.00"))

    statement = report["income_statement"]
    assert _row(statement["revenues"], "8400")["amount"] == ZERO
    assert _row(statement["expenses"], "4900")["amount"] == ZERO
    assert statement["totals"]["net_income"] == ZERO
    assert report["eliminations"]["income_expense"]["pairs"][0]["is_matched"] is True
    assert report["warnings"] == []

    trial = report["trial_balance"]
    assert _row(trial["rows"], "1400")["amount"] == ZERO
    assert _row(trial["rows"], "8400")["amount"] == ZERO
    assert trial["totals"]["is_balanced"] is True


def test_different_balances_stay_visible_as_difference(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _sale(session, parent, _partner_for(session, parent, child), "100")
    _purchase(session, child, _partner_for(session, child, parent), "90")
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    sheet = report["balance_sheet"]
    difference = _row(sheet["assets"], DIFFERENCE_DEBT_CODE)
    assert difference["amount"] == Decimal("10.00")
    assert difference["name"] == "Aufrechnungsdifferenz (Schulden)"
    assert difference["is_difference"] is True
    assert _row(sheet["assets"], "1400")["amount"] == ZERO
    assert _row(sheet["liabilities_and_equity"], "1600")["amount"] == ZERO
    assert sheet["totals"]["is_balanced"] is True
    assert sheet["totals"]["intercompany_difference"] == Decimal("10.00")
    assert sheet["totals"]["total_assets"] == Decimal("10.00")
    assert _row(sheet["liabilities_and_equity"], "P&L")["amount"] == Decimal("10.00")

    pair = report["eliminations"]["debt"]["pairs"][0]
    assert pair["is_matched"] is False
    assert pair["difference"] == Decimal("10.00")
    assert {line["company_name"] for line in pair["lines"]} == {"Mutter GmbH", "Tochter GmbH"}

    statement = report["income_statement"]
    income_difference = _row(statement["revenues"], DIFFERENCE_INCOME_CODE)
    assert income_difference["amount"] == Decimal("10.00")
    assert statement["totals"]["net_income"] == Decimal("10.00")
    assert any("Aufrechnungsdifferenz Schulden" in warning for warning in report["warnings"])
    assert any("Aufrechnungsdifferenz Aufwand/Ertrag" in warning for warning in report["warnings"])

    trial = report["trial_balance"]
    assert _row(trial["rows"], DIFFERENCE_DEBT_CODE)["amount"] == Decimal("10.00")
    assert _row(trial["rows"], DIFFERENCE_INCOME_CODE)["amount"] == Decimal("-10.00")
    assert trial["totals"]["is_balanced"] is True


def test_negative_difference_goes_to_the_liability_side(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _sale(session, parent, _partner_for(session, parent, child), "90")
    _purchase(session, child, _partner_for(session, child, parent), "100")
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    sheet = report["balance_sheet"]
    assert _row(sheet["assets"], DIFFERENCE_DEBT_CODE) is None
    assert _row(sheet["liabilities_and_equity"], DIFFERENCE_DEBT_CODE)["amount"] == Decimal("10.00")
    assert sheet["totals"]["is_balanced"] is True
    statement = report["income_statement"]
    assert _row(statement["expenses"], DIFFERENCE_INCOME_CODE)["amount"] == Decimal("10.00")
    assert statement["totals"]["net_income"] == Decimal("-10.00")


def test_one_sided_booking_is_a_difference(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _sale(session, parent, _partner_for(session, parent, child), "50")
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    pair = report["eliminations"]["debt"]["pairs"][0]
    assert (pair["balance_a"], pair["balance_b"], pair["difference"]) == (
        Decimal("50.00"),
        ZERO,
        Decimal("50.00"),
    )
    assert report["balance_sheet"]["totals"]["is_balanced"] is True


def test_three_companies_report_each_pair(session: Session) -> None:
    tenant, parent, first = _two_companies(session)
    second = _company(session, tenant, "Zweite Tochter GmbH")
    _capital(session, parent, "1000")
    _capital(session, first, "400")
    _capital(session, second, "200")
    # Mutter -> Tochter 1: geht auf
    _sale(session, parent, _partner_for(session, parent, first), "100")
    _purchase(session, first, _partner_for(session, first, parent), "100")
    # Tochter 1 -> Tochter 2: 5 Differenz
    _sale(session, first, _partner_for(session, first, second), "80")
    _purchase(session, second, _partner_for(session, second, first), "75")
    # Mutter -> Tochter 2: -3 Differenz
    _sale(session, parent, _partner_for(session, parent, second), "30")
    _purchase(session, second, _partner_for(session, second, parent), "33")
    group = _group(session, parent, first, second)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    sheet = report["balance_sheet"]
    assert sheet["totals"]["is_balanced"] is True
    pairs = report["eliminations"]["debt"]["pairs"]
    assert len(pairs) == 3
    differences = {
        frozenset((pair["company_a_id"], pair["company_b_id"])): pair["difference"]
        for pair in pairs
    }
    assert differences[frozenset((parent.id, first.id))] == ZERO
    assert differences[frozenset((first.id, second.id))] == Decimal("5.00")
    assert differences[frozenset((parent.id, second.id))] == Decimal("-3.00")
    assert sheet["totals"]["intercompany_difference"] == Decimal("2.00")
    assert _row(sheet["assets"], DIFFERENCE_DEBT_CODE)["amount"] == Decimal("2.00")
    assert _row(sheet["assets"], "1400")["amount"] == ZERO
    assert sheet["totals"]["total_assets"] == Decimal("1602.00")
    statement = report["income_statement"]
    assert _row(statement["revenues"], "8400")["amount"] == ZERO
    assert _row(statement["expenses"], "4900")["amount"] == ZERO
    assert statement["totals"]["net_income"] == Decimal("2.00")


def test_payment_settles_the_elimination(session: Session) -> None:
    _, parent, child = _two_companies(session)
    partner_at_parent = _partner_for(session, parent, child)
    partner_at_child = _partner_for(session, child, parent)
    _capital(session, child, "500")
    _sale(session, parent, partner_at_parent, "100")
    _purchase(session, child, partner_at_child, "100")
    _book(
        session,
        parent,
        [("1200", "100", "0", None), ("1400", "0", "100", partner_at_parent)],
    )
    _book(
        session,
        child,
        [("1600", "100", "0", partner_at_child), ("1200", "0", "100", None)],
    )
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    assert report["eliminations"]["debt"]["pairs"] == []
    sheet = report["balance_sheet"]
    assert _row(sheet["assets"], "1200")["amount"] == Decimal("500.00")
    assert sheet["totals"]["is_balanced"] is True
    # Die Erfolgszeilen bleiben eliminiert, auch wenn die Rechnung bezahlt ist.
    assert report["income_statement"]["totals"]["net_income"] == ZERO
    assert report["eliminations"]["income_expense"]["pairs"][0]["is_matched"] is True


def test_external_partner_and_other_lines_are_not_eliminated(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _sale(session, parent, _partner_for(session, parent, None), "70")
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    assert _row(report["balance_sheet"]["assets"], "1400")["amount"] == Decimal("70.00")
    assert _row(report["income_statement"]["revenues"], "8400")["amount"] == Decimal("70.00")
    assert report["eliminations"]["income_expense"]["pairs"] == []


def test_partner_on_income_line_decides_and_mixed_entries_warn(session: Session) -> None:
    _, parent, child = _two_companies(session)
    connected = _partner_for(session, parent, child)
    outsider = _partner_for(session, parent, None)
    # Partner direkt an der Erfolgszeile: Direktbuchung Aufwand an Bank.
    _book(session, parent, [("4900", "40", "0", connected), ("1200", "0", "40", None)])
    # Erfolgszeile ohne Partner in einer Buchung mit verbundenem und fremdem Partner.
    mixed = _book(
        session,
        parent,
        [
            ("1400", "60", "0", connected),
            ("1400", "40", "0", outsider),
            ("8400", "0", "100", None),
        ],
    )
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    statement = report["income_statement"]
    assert _row(statement["expenses"], "4900")["amount"] == ZERO
    assert _row(statement["revenues"], "8400")["amount"] == Decimal("100.00")
    assert any(mixed.posting_number in warning for warning in report["warnings"])
    assert report["balance_sheet"]["totals"]["is_balanced"] is True


def test_reversal_nets_out_of_the_elimination(session: Session) -> None:
    _, parent, child = _two_companies(session)
    entry = _sale(session, parent, _partner_for(session, parent, child), "100")
    reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 3, 2),
        changed_by="t",
    )
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    assert report["eliminations"]["debt"]["pairs"] == []
    assert report["eliminations"]["income_expense"]["pairs"] == []
    assert report["balance_sheet"]["totals"]["is_balanced"] is True


# ---------------------------------------------------------------------------
# Einbezogen ab/bis
# ---------------------------------------------------------------------------


def test_included_from_limits_period_reports_and_elimination(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, parent, "1000")
    _capital(session, child, "500")
    partner_at_parent = _partner_for(session, parent, child)
    partner_at_child = _partner_for(session, child, parent)
    # Vor der Einbeziehung: Lieferung bleibt in der GuV der Mutter stehen.
    _sale(session, parent, partner_at_parent, "100", entry_date=date(2026, 3, 1))
    _purchase(session, child, partner_at_child, "100", entry_date=date(2026, 3, 1))
    # Danach: wird eliminiert.
    _sale(session, parent, partner_at_parent, "30", entry_date=date(2026, 8, 1))
    _purchase(session, child, partner_at_child, "30", entry_date=date(2026, 8, 1))
    group = _group(session, parent)
    set_member(
        session=session,
        group=group,
        changed_by="t",
        company_id=child.id,
        included_from="2026-07-01",
    )
    session.commit()

    report = consolidation_report(
        session=session, group=group, date_from=date(2026, 1, 1), date_to=date(2026, 12, 31)
    )

    statement = report["income_statement"]
    revenue = _row(statement["revenues"], "8400")
    assert revenue["amounts"][parent.id] == Decimal("130.00")
    assert revenue["eliminations"] == Decimal("-30.00")
    assert revenue["amount"] == Decimal("100.00")
    expense = _row(statement["expenses"], "4900")
    # Die Tochter zählt in der GuV erst ab der Einbeziehung.
    assert expense["amounts"][child.id] == Decimal("30.00")
    assert expense["amount"] == ZERO
    # Die Bilanz zeigt den vollen Bestand zum Stichtag, beide Salden werden aufgerechnet.
    sheet = report["balance_sheet"]
    assert sheet["totals"]["is_balanced"] is True
    assert _row(sheet["assets"], "1400")["amount"] == ZERO
    assert report["eliminations"]["debt"]["pairs"][0]["is_matched"] is True
    assert any("einbezogen ab 01.07.2026" in note for note in report["notes"])


def test_member_outside_the_balance_sheet_date_is_left_out(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, child, "500")
    _sale(session, parent, _partner_for(session, parent, child), "100")
    _purchase(session, child, _partner_for(session, child, parent), "100")
    group = _group(session, parent)
    set_member(
        session=session,
        group=group,
        changed_by="t",
        company_id=child.id,
        included_to="2026-06-30",
    )
    session.commit()

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    company = next(item for item in report["companies"] if item["company_id"] == child.id)
    assert company["in_balance_sheet"] is False
    sheet = report["balance_sheet"]
    # Die Forderung an die ausgeschiedene Tochter ist nun eine fremde Forderung.
    assert _row(sheet["assets"], "1400")["amount"] == Decimal("100.00")
    assert sheet["summary"]["assets"]["amounts"][child.id] == ZERO
    assert sheet["totals"]["is_balanced"] is True
    assert any("nicht im Konsolidierungskreis" in warning for warning in report["warnings"])
    assert any("fehlt in der Bilanz" in note for note in report["notes"])
    # Im Zeitraum der Einbeziehung werden die Erfolgszeilen weiter eliminiert.
    assert _row(report["income_statement"]["revenues"], "8400")["amount"] == ZERO

    before_exit = consolidation_report(session=session, group=group, date_to=date(2026, 6, 30))
    assert _row(before_exit["balance_sheet"]["assets"], "1400")["amount"] == ZERO
    assert before_exit["warnings"] == []


def test_member_without_overlap_is_left_out_of_the_period(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, child, "500")
    group = _group(session, parent)
    set_member(
        session=session,
        group=group,
        changed_by="t",
        company_id=child.id,
        included_from="2027-01-01",
    )
    session.commit()

    report = consolidation_report(
        session=session, group=group, date_from=date(2026, 1, 1), date_to=date(2026, 12, 31)
    )

    company = next(item for item in report["companies"] if item["company_id"] == child.id)
    assert company["in_period"] is False
    assert report["trial_balance"]["rows"] == []
    assert report["balance_sheet"]["totals"]["total_assets"] == ZERO


# ---------------------------------------------------------------------------
# Saldovorträge und Abschlussbuchungen
# ---------------------------------------------------------------------------


def test_closing_entries_follow_the_switch(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, parent, "1000")
    _book(
        session,
        parent,
        [("4900", "200", "0", None), ("1200", "0", "200", None)],
        entry_date=date(2026, 12, 31),
        closing=True,
    )
    group = _group(session, parent, child)

    without = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))
    including = consolidation_report(
        session=session, group=group, date_to=date(2026, 12, 31), include_closing_entries=True
    )

    assert without["income_statement"]["totals"]["net_income"] == ZERO
    assert including["income_statement"]["totals"]["net_income"] == Decimal("-200.00")
    assert without["balance_sheet"]["totals"]["total_assets"] == Decimal("1000.00")
    assert including["balance_sheet"]["totals"]["total_assets"] == Decimal("800.00")
    assert including["balance_sheet"]["totals"]["is_balanced"] is True


def _close_year(session: Session, company: Company) -> None:
    fiscal_year = session.execute(
        select(FiscalYear).where(FiscalYear.company_id == company.id)
    ).scalar_one()
    close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="t")
    session.commit()


def test_year_end_carryforward_matches_single_reports(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, parent, "1000")
    _capital(session, child, "500")
    _sale(session, parent, _partner_for(session, parent, child), "100")
    _purchase(session, child, _partner_for(session, child, parent), "100")
    _close_year(session, parent)
    _close_year(session, child)
    group = _group(session, parent, child)

    # Folgejahr mit Saldovortrag: Die Salden bleiben eliminiert, Vortrag zählt genau einmal.
    report = consolidation_report(
        session=session, group=group, date_from=date(2027, 1, 1), date_to=date(2027, 12, 31)
    )
    sheet = report["balance_sheet"]
    assert sheet["totals"]["is_balanced"] is True
    assert sheet["totals"]["total_assets"] == Decimal("1500.00")
    assert _row(sheet["assets"], "1400")["total"] == Decimal("100.00")
    assert _row(sheet["assets"], "1400")["amount"] == ZERO
    assert report["income_statement"]["totals"]["net_income"] == ZERO
    trial = report["trial_balance"]
    assert trial["totals"]["is_balanced"] is True
    assert _row(trial["rows"], "1400")["amounts"][parent.id] == Decimal("100.00")
    assert _row(trial["rows"], "1400")["amount"] == ZERO
    assert _row(trial["rows"], "1200")["amount"] == Decimal("1500.00")

    # Abgeschlossenes Jahr mit Abschlussbuchungen: Erfolgskonten sind glattgestellt, die
    # SuSa eliminiert dort keine Erträge mehr, sie bleibt ausgeglichen.
    closed = consolidation_report(
        session=session,
        group=group,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 12, 31),
        include_closing_entries=True,
    )
    assert closed["trial_balance"]["totals"]["is_balanced"] is True
    assert _row(closed["trial_balance"]["rows"], "8400")["amount"] == ZERO
    assert _row(closed["trial_balance"]["rows"], "8400")["eliminations"] == ZERO
    # Die GuV lässt den Ergebnisvortrag immer außen vor und eliminiert weiter.
    assert _row(closed["income_statement"]["revenues"], "8400")["eliminations"] == Decimal(
        "-100.00"
    )
    assert closed["balance_sheet"]["totals"]["is_balanced"] is True


# ---------------------------------------------------------------------------
# Kontenrahmen und Kontoarten
# ---------------------------------------------------------------------------


def test_account_type_conflict_warns_and_stays_balanced(session: Session) -> None:
    _, parent, child = _two_companies(session)
    session.add(
        Account(
            tenant_id=parent.tenant_id,
            company_id=parent.id,
            code="3000",
            name="Rohstoffe",
            account_type="asset",
        )
    )
    session.add(
        Account(
            tenant_id=child.tenant_id,
            company_id=child.id,
            code="3000",
            name="Wareneingang",
            account_type="expense",
        )
    )
    session.commit()
    _book(session, parent, [("3000", "10", "0", None), ("1200", "0", "10", None)])
    _book(session, child, [("3000", "20", "0", None), ("1200", "0", "20", None)])
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    assert any("Konto 3000" in warning for warning in report["warnings"])
    assert report["balance_sheet"]["totals"]["is_balanced"] is True
    # Die Kontoart der Muttergesellschaft zählt.
    assert _row(report["balance_sheet"]["assets"], "3000")["amount"] == Decimal("30.00")
    assert _row(report["balance_sheet"]["assets"], "3000")["name"] == "Rohstoffe"


def test_income_and_revenue_are_the_same_account_type(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _account(session, child, "8400").account_type = "revenue"
    session.commit()
    _book(session, parent, [("1200", "10", "0", None), ("8400", "0", "10", None)])
    _book(session, child, [("1200", "20", "0", None), ("8400", "0", "20", None)])
    group = _group(session, parent, child)

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    assert report["warnings"] == []
    assert report["income_statement"]["totals"]["total_revenue"] == Decimal("30.00")


def test_different_charts_warn(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)
    charts = {parent.id: "skr03", child.id: "skr04"}
    monkeypatch.setattr(
        "app.services.consolidation.detect_company_chart",
        lambda *, session, company_id: charts[company_id],
    )

    report = consolidation_report(session=session, group=group)

    assert any("Kontenrahmen" in warning for warning in report["warnings"])


# ---------------------------------------------------------------------------
# Beteiligungsquote
# ---------------------------------------------------------------------------


def test_ownership_below_full_only_informs(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, child, "500")
    group = _group(session, parent)
    set_member(
        session=session,
        group=group,
        changed_by="t",
        company_id=child.id,
        ownership_percent="60",
    )
    session.commit()

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    company = next(item for item in report["companies"] if item["company_id"] == child.id)
    assert company["ownership_percent"] == Decimal("60.00")
    assert company["minority_percent"] == Decimal("40.00")
    assert any("Fremdanteil 40,00 %" in note for note in report["notes"])
    # Es wird nichts berechnet: Das Kapital der Tochter steht voll im Bericht.
    assert _row(report["balance_sheet"]["liabilities_and_equity"], "0800")["amount"] == Decimal(
        "500.00"
    )


# ---------------------------------------------------------------------------
# Manuelle Konsolidierungsbuchungen
# ---------------------------------------------------------------------------


def _capital_adjustment(session: Session, group: ConsolidationGroup, **overrides):
    values = {
        "adjustment_date": "2026-01-02",
        "kind": "capital",
        "description": "Kapitalkonsolidierung",
        "lines": [
            {"account_code": "0800", "debit_amount": "25000", "credit_amount": "0"},
            {"account_code": "0500", "debit_amount": "0", "credit_amount": "25000"},
        ],
    }
    values.update(overrides)
    return create_adjustment(session=session, group=group, changed_by="t", **values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "lines": [
                    {"account_code": "0800", "debit_amount": "100"},
                    {"account_code": "0500", "credit_amount": "90"},
                ]
            },
            "nicht ausgeglichen",
        ),
        ({"lines": [{"account_code": "0800", "debit_amount": "100"}]}, "zwei Zeilen"),
        ({"lines": "kaputt"}, "zwei Zeilen"),
        (
            {
                "lines": [
                    {"account_code": "0800", "debit_amount": "100", "credit_amount": "100"},
                    {"account_code": "0500", "credit_amount": "100"},
                ]
            },
            "genau einer",
        ),
        (
            {
                "lines": [
                    {"account_code": "9999", "debit_amount": "100"},
                    {"account_code": "0500", "credit_amount": "100"},
                ]
            },
            "Konto 9999",
        ),
        (
            {
                "lines": [
                    {"account_code": "0800", "debit_amount": "-100"},
                    {"account_code": "0500", "credit_amount": "100"},
                ]
            },
            "negativ",
        ),
        (
            {
                "lines": [
                    {"account_code": "0800", "debit_amount": "100.001"},
                    {"account_code": "0500", "credit_amount": "100.001"},
                ]
            },
            "Nachkommastellen",
        ),
        ({"kind": "steuer"}, "capital oder other"),
        ({"adjustment_date": None}, "Datum"),
        ({"adjustment_date": "gestern"}, "Datum"),
        ({"description": "  "}, "Text"),
    ],
)
def test_invalid_adjustments_are_rejected(
    session: Session, overrides: dict, message: str
) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)

    with pytest.raises(ConsolidationError, match=message):
        _capital_adjustment(session, group, **overrides)

    session.rollback()
    assert group.adjustments == []


def test_adjustment_changes_only_the_report_and_is_audited(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _book(
        session,
        parent,
        [
            ("0500", "25000", "0", None),
            ("1200", "0", "25000", None),
        ],
    )
    _capital(session, parent, "30000")
    _capital(session, child, "25000")
    group = _group(session, parent, child)

    entries_before = session.execute(select(JournalEntry.id)).scalars().all()
    adjustment = _capital_adjustment(session, group)
    session.commit()
    assert session.execute(select(JournalEntry.id)).scalars().all() == entries_before

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    sheet = report["balance_sheet"]
    assert _row(sheet["assets"], "0500")["total"] == Decimal("25000.00")
    assert _row(sheet["assets"], "0500")["adjustments"] == Decimal("-25000.00")
    assert _row(sheet["assets"], "0500")["amount"] == ZERO
    capital = _row(sheet["liabilities_and_equity"], "0800")
    assert capital["total"] == Decimal("55000.00")
    assert capital["adjustments"] == Decimal("-25000.00")
    assert capital["amount"] == Decimal("30000.00")
    assert sheet["totals"]["is_balanced"] is True
    assert report["trial_balance"]["totals"]["is_balanced"] is True
    assert report["adjustments"][0]["kind_label"] == "Kapitalkonsolidierung"
    assert balance_sheet_for_company(
        session=session, company_id=parent.id, date_to=date(2026, 12, 31)
    )["totals"]["total_assets"] == Decimal("30000.00")

    adjustment_id = adjustment.id
    delete_adjustment(session=session, adjustment=adjustment, changed_by="t", reason="Tippfehler")
    session.commit()
    entries = session.execute(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "consolidation_adjustment",
            AuditLog.entity_id == str(adjustment_id),
        )
        .order_by(AuditLog.id)
    ).scalars().all()
    assert [entry.action for entry in entries] == ["created", "deleted"]
    assert entries[1].payload["reason"] == "Tippfehler"
    assert entries[1].payload["before"]["lines"][0]["account_code"] == "0800"
    assert entries[1].company_id == parent.id

    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))
    assert report["adjustments"] == []
    assert _row(report["balance_sheet"]["assets"], "0500")["amount"] == Decimal("25000.00")


def test_adjustment_dates_decide_where_it_counts(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _capital(session, parent, "1000")
    group = _group(session, parent, child)
    # Vorjahresbuchung mit Erfolgskonto: wirkt später als Vortrag, nicht als Jahresergebnis.
    _book(
        session,
        parent,
        [("1200", "1", "0", None), ("8400", "0", "1", None)],
        entry_date=date(2027, 2, 1),
    )
    create_adjustment(
        session=session,
        group=group,
        changed_by="t",
        adjustment_date="2026-06-30",
        kind="other",
        description="Rückstellung",
        lines=[
            {"account_code": "4900", "debit_amount": "40"},
            {"account_code": "1600", "credit_amount": "40"},
        ],
    )
    session.commit()

    in_year = consolidation_report(
        session=session, group=group, date_from=date(2026, 1, 1), date_to=date(2026, 12, 31)
    )
    assert _row(in_year["income_statement"]["expenses"], "4900")["adjustments"] == Decimal("40.00")
    assert in_year["income_statement"]["totals"]["net_income"] == Decimal("-40.00")
    assert in_year["balance_sheet"]["totals"]["is_balanced"] is True
    assert _row(in_year["balance_sheet"]["liabilities_and_equity"], "1600")["amount"] == Decimal(
        "40.00"
    )

    before = consolidation_report(session=session, group=group, date_to=date(2026, 3, 31))
    assert _row(before["balance_sheet"]["liabilities_and_equity"], "1600") is None

    next_year = consolidation_report(
        session=session, group=group, date_from=date(2027, 1, 1), date_to=date(2027, 12, 31)
    )
    assert _row(next_year["income_statement"]["expenses"], "4900") is None
    assert next_year["income_statement"]["totals"]["net_income"] == Decimal("1.00")
    sheet = next_year["balance_sheet"]
    assert sheet["totals"]["is_balanced"] is True
    assert _row(sheet["liabilities_and_equity"], "1600")["amount"] == Decimal("40.00")
    assert _row(sheet["liabilities_and_equity"], CARRY_CODE)["amount"] == Decimal("-40.00")
    assert _row(sheet["liabilities_and_equity"], "P&L")["amount"] == Decimal("1.00")
    trial = next_year["trial_balance"]
    assert trial["totals"]["is_balanced"] is True
    assert _row(trial["rows"], CARRY_CODE)["amount"] == Decimal("40.00")
    assert _row(trial["rows"], "1600")["amount"] == Decimal("-40.00")


def test_adjustments_need_an_active_group(session: Session) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)
    update_group(session=session, group=group, changed_by="t", is_active=False)

    with pytest.raises(ConsolidationError, match="inaktiv"):
        _capital_adjustment(session, group)


def test_invalid_period_is_rejected(session: Session) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)

    with pytest.raises(ConsolidationError, match="Von-Datum"):
        consolidation_report(
            session=session, group=group, date_from=date(2026, 2, 1), date_to=date(2026, 1, 1)
        )


def test_csv_and_json_form(session: Session) -> None:
    _, parent, child = _two_companies(session)
    _sale(session, parent, _partner_for(session, parent, child), "100")
    _purchase(session, child, _partner_for(session, child, parent), "90")
    group = _group(session, parent, child)
    report = consolidation_report(session=session, group=group, date_to=date(2026, 12, 31))

    rows = list(csv.reader(io.StringIO(consolidation_report_csv(report))))

    assert rows[0][:4] == ["Bereich", "Konto", "Bezeichnung", "Kontoart"]
    assert rows[0][4:6] == ["Mutter GmbH", "Tochter GmbH"]
    assert rows[0][-4:] == ["Summe", "Eliminierung", "Manuelle Buchung", "Konsolidiert"]
    difference = next(
        row for row in rows if row[0] == "Bilanz Aktiva" and row[1] == DIFFERENCE_DEBT_CODE
    )
    assert difference[-1] == "10.00"
    assert difference[-3] == "10.00"

    payload = json_ready(report)
    json.dumps(payload)
    assert payload["balance_sheet"]["totals"]["total_assets"] == "10.00"
    assert payload["balance_sheet"]["assets"][0]["amounts"][str(parent.id)] == "100.00"


def test_audit_package_contains_groups_of_the_parent(session: Session) -> None:
    _, parent, child = _two_companies(session)
    group = _group(session, parent, child)
    _capital_adjustment(session, group)
    session.commit()

    package = build_audit_export_package(
        session=session,
        company_id=parent.id,
        generated_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
        include_documents=False,
    )
    other = build_audit_export_package(
        session=session,
        company_id=child.id,
        generated_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
        include_documents=False,
    )

    with zipfile.ZipFile(io.BytesIO(package.payload)) as archive:
        assert json.loads(archive.read("data/consolidation_groups.json"))[0]["id"] == group.id
        assert len(json.loads(archive.read("data/consolidation_members.json"))) == 2
        adjustments = json.loads(archive.read("data/consolidation_adjustments.json"))
        assert adjustments[0]["kind"] == "capital"
        assert len(json.loads(archive.read("data/consolidation_adjustment_lines.json"))) == 2
        history = json.loads(archive.read("data/consolidation_history.json"))
        assert {entry["entity_type"] for entry in history} == {
            "consolidation_group",
            "consolidation_adjustment",
        }
        catalog = json.loads(archive.read("schema/field_catalog.json"))
    assert "consolidation_groups" in json.dumps(catalog)
    with zipfile.ZipFile(io.BytesIO(other.payload)) as archive:
        assert json.loads(archive.read("data/consolidation_groups.json")) == []


# ---------------------------------------------------------------------------
# REST-API, MCP und Oberfläche
# ---------------------------------------------------------------------------


def _app(tmp_path: Path, **config):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'consolidation.db'}",
            **config,
        }
    )


def _seed_api_world(app) -> dict[str, int]:
    """Zwei Mandanten; Mandant A mit zwei verflochtenen Gesellschaften und Benutzern."""
    with app.extensions["db_session_factory"]() as db:
        tenant_a = _tenant(db, "A")
        parent = _company(db, tenant_a, "Mutter GmbH")
        child = _company(db, tenant_a, "Tochter GmbH")
        tenant_b = _tenant(db, "B")
        foreign = _company(db, tenant_b, "Fremde GmbH")
        _sale(db, parent, _partner_for(db, parent, child), "100")
        _purchase(db, child, _partner_for(db, child, parent), "90")
        for username, role, token, tenant in (
            ("buchhalter-a", "Buchhalter", "obk_buchhalter-a", tenant_a),
            ("pruefer-a", "Pruefer", "obk_pruefer-a", tenant_a),
            ("support-a", "Support", "obk_support-a", tenant_a),
            ("buchhalter-b", "Buchhalter", "obk_buchhalter-b", tenant_b),
            ("global-admin", "Admin", "obk_global-admin", None),
        ):
            db.add(
                User(
                    username=username,
                    password_hash=hash_password("passwort1234"),
                    role=role,
                    tenant_id=tenant.id if tenant else None,
                    api_token_hash=hash_api_token(token),
                )
            )
        db.commit()
        return {
            "tenant_a": tenant_a.id,
            "parent": parent.id,
            "child": child.id,
            "foreign": foreign.id,
        }


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_api_flow_rights_and_tenant_scope(tmp_path: Path) -> None:
    app = _app(tmp_path, API_REQUIRE_AUTH=True)
    world = _seed_api_world(app)
    client = app.test_client()
    accountant = _bearer("obk_buchhalter-a")
    auditor = _bearer("obk_pruefer-a")
    support = _bearer("obk_support-a")
    outsider = _bearer("obk_buchhalter-b")
    admin = _bearer("obk_global-admin")

    body = {
        "name": "Konzern",
        "parent_company_id": world["parent"],
        "members": [{"company_id": world["child"], "ownership_percent": "80"}],
    }
    groups_url = "/api/v1/consolidation-groups"
    assert client.post(groups_url, json=body, headers=auditor).status_code == 403
    assert client.post(groups_url, json=body, headers=support).status_code == 403
    # Fremde Muttergesellschaft bzw. fremdes Mitglied.
    assert client.post(groups_url, json=body, headers=outsider).status_code == 404
    response = client.post(
        groups_url,
        json={**body, "members": [{"company_id": world["foreign"]}]},
        headers=accountant,
    )
    assert response.status_code == 422
    assert "Fremde" not in response.get_json()["error"]
    assert client.post(groups_url, json={**body, "bogus": 1}, headers=accountant).status_code == 400
    assert client.post(groups_url, json={"name": "x"}, headers=accountant).status_code == 400

    created = client.post(groups_url, json=body, headers=accountant)
    assert created.status_code == 201, created.get_json()
    group = created.get_json()
    group_id = group["id"]
    assert group["parent_company_name"] == "Mutter GmbH"
    assert [member["company_id"] for member in group["members"]] == [
        world["parent"],
        world["child"],
    ]
    assert group["members"][1]["ownership_percent"] == "80.00"

    # Sichtbarkeit: eigener Mandant und globaler Admin.
    listed = client.get(groups_url, headers=auditor).get_json()["groups"]
    assert [item["id"] for item in listed] == [group_id]
    assert client.get(groups_url, headers=outsider).get_json()["groups"] == []
    assert client.get(groups_url, headers=admin).get_json()["groups"][0]["id"] == group_id
    for path in (
        f"/api/v1/consolidation-groups/{group_id}",
        f"/api/v1/consolidation-groups/{group_id}/report",
        f"/api/v1/consolidation-groups/{group_id}/adjustments",
        f"/api/v1/exports/consolidation-report.csv?group_id={group_id}",
    ):
        assert client.get(path, headers=outsider).status_code == 404, path
        assert client.get(path, headers=auditor).status_code == 200, path
    assert (
        client.patch(
            f"/api/v1/consolidation-groups/{group_id}", json={"name": "x"}, headers=outsider
        ).status_code
        == 404
    )

    # Bericht
    report = client.get(
        f"/api/v1/consolidation-groups/{group_id}/report?date_to=2026-12-31",
        headers=auditor,
    ).get_json()
    assert report["balance_sheet"]["totals"]["is_balanced"] is True
    assert report["balance_sheet"]["totals"]["total_assets"] == "10.00"
    assert report["eliminations"]["debt"]["pairs"][0]["difference"] == "10.00"
    assert report["companies"][1]["minority_percent"] == "20.00"
    assert report["limits"]
    assert (
        client.get(
            f"/api/v1/consolidation-groups/{group_id}/report?date_to=kaputt", headers=auditor
        ).status_code
        == 400
    )
    assert (
        client.get(
            f"/api/v1/consolidation-groups/{group_id}/report"
            "?date_from=2026-12-31&date_to=2026-01-01",
            headers=auditor,
        ).status_code
        == 422
    )

    # Änderung
    assert (
        client.patch(
            f"/api/v1/consolidation-groups/{group_id}", json={"name": "Neu"}, headers=auditor
        ).status_code
        == 403
    )
    patched = client.patch(
        f"/api/v1/consolidation-groups/{group_id}",
        json={"name": "Neu", "members": [{"company_id": world["parent"]}]},
        headers=accountant,
    )
    assert patched.status_code == 200, patched.get_json()
    assert patched.get_json()["changed"] is True
    assert len(patched.get_json()["members"]) == 1
    patched = client.patch(
        f"/api/v1/consolidation-groups/{group_id}",
        json={"members": [{"company_id": world["child"]}]},
        headers=accountant,
    )
    assert patched.status_code == 422
    assert (
        client.patch(
            f"/api/v1/consolidation-groups/{group_id}", json={"tenant_id": 1}, headers=accountant
        ).status_code
        == 400
    )
    assert (
        client.patch(
            f"/api/v1/consolidation-groups/{group_id}", json={}, headers=accountant
        ).status_code
        == 400
    )
    assert (
        client.patch(
            f"/api/v1/consolidation-groups/{group_id}",
            json={
                "members": [
                    {"company_id": world["parent"]},
                    {"company_id": world["child"]},
                ]
            },
            headers=accountant,
        ).status_code
        == 200
    )

    # Manuelle Buchung
    adjustment_body = {
        "adjustment_date": "2026-06-30",
        "kind": "other",
        "description": "Umgliederung",
        "lines": [
            {"account_code": "1400", "debit_amount": "5.00"},
            {"account_code": "1600", "credit_amount": "5.00"},
        ],
    }
    url = f"/api/v1/consolidation-groups/{group_id}/adjustments"
    assert client.post(url, json=adjustment_body, headers=auditor).status_code == 403
    assert client.post(url, json=adjustment_body, headers=outsider).status_code == 404
    unbalanced = client.post(
        url,
        json={
            **adjustment_body,
            "lines": [
                {"account_code": "1400", "debit_amount": "5.00"},
                {"account_code": "1600", "credit_amount": "4.00"},
            ],
        },
        headers=accountant,
    )
    assert unbalanced.status_code == 422
    assert "nicht ausgeglichen" in unbalanced.get_json()["error"]
    created_adjustment = client.post(url, json=adjustment_body, headers=accountant)
    assert created_adjustment.status_code == 201, created_adjustment.get_json()
    adjustment_id = created_adjustment.get_json()["id"]
    assert created_adjustment.get_json()["lines"][0]["account_name"].startswith("Forderungen")
    assert client.get(url, headers=auditor).get_json()["adjustments"][0]["id"] == adjustment_id
    assert (
        client.get(f"/api/v1/consolidation-groups/{group_id}", headers=auditor).get_json()[
            "adjustments"
        ][0]["id"]
        == adjustment_id
    )

    # CSV und Download-Link
    csv_response = client.get(
        f"/api/v1/exports/consolidation-report.csv?group_id={group_id}&date_to=2026-12-31",
        headers=auditor,
    )
    assert csv_response.status_code == 200
    assert csv_response.mimetype == "text/csv"
    assert "Aufrechnungsdifferenz (Schulden)" in csv_response.get_data(as_text=True)
    link = client.get(
        f"/api/v1/exports/consolidation-report.csv?group_id={group_id}&download_link=1",
        headers=auditor,
    )
    assert link.status_code == 200
    assert link.get_json()["download_path"].startswith("/api/v1/downloads/")
    assert client.get(link.get_json()["download_path"]).status_code == 200
    assert (
        client.get("/api/v1/exports/consolidation-report.csv", headers=auditor).status_code == 400
    )

    # Löschen
    delete_url = f"{url}/{adjustment_id}/delete"
    assert client.post(delete_url, json={}, headers=auditor).status_code == 403
    assert client.post(delete_url, json={}, headers=outsider).status_code == 404
    assert client.post(f"{url}/999/delete", json={}, headers=accountant).status_code == 404
    deleted = client.post(delete_url, json={"reason": "Fehler"}, headers=accountant)
    assert deleted.status_code == 200
    assert client.get(url, headers=auditor).get_json()["adjustments"] == []
    assert client.post(delete_url, json={}, headers=accountant).status_code == 404


def test_partner_api_carries_intercompany_field(tmp_path: Path) -> None:
    app = _app(tmp_path, API_REQUIRE_AUTH=True)
    world = _seed_api_world(app)
    client = app.test_client()
    accountant = _bearer("obk_buchhalter-a")

    created = client.post(
        "/api/v1/partners",
        json={
            "company_id": world["parent"],
            "name": "Neue Tochter",
            "is_customer": True,
            "intercompany_company_id": world["child"],
        },
        headers=accountant,
    )
    assert created.status_code == 201, created.get_json()
    partner_id = created.get_json()["id"]
    assert created.get_json()["intercompany_company_id"] == world["child"]
    assert (
        client.get(f"/api/v1/partners/{partner_id}", headers=accountant).get_json()[
            "intercompany_company_id"
        ]
        == world["child"]
    )

    rejected = client.patch(
        f"/api/v1/partners/{partner_id}",
        json={"intercompany_company_id": world["foreign"]},
        headers=accountant,
    )
    assert rejected.status_code == 400
    assert "Fremde" not in rejected.get_json()["error"]
    cleared = client.patch(
        f"/api/v1/partners/{partner_id}",
        json={"intercompany_company_id": None},
        headers=accountant,
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["intercompany_company_id"] is None


def _login(app, username: str):
    client = app.test_client()
    response = client.post(
        "/auth/login", data={"username": username, "password": "passwort1234"}
    )
    assert response.status_code == 302
    return client


def test_ui_flow_and_read_only_roles(tmp_path: Path) -> None:
    app = _app(tmp_path)
    world = _seed_api_world(app)
    client = _login(app, "buchhalter-a")

    empty = client.get("/konsolidierung")
    assert empty.status_code == 200
    assert "Noch keine Gruppe" in empty.get_data(as_text=True)
    assert 'href="/konsolidierung' in client.get("/berichte").get_data(as_text=True)

    created = client.post(
        "/konsolidierung",
        data={"name": "Testgruppe A", "parent_company_id": world["parent"]},
    )
    assert created.status_code == 302
    assert "group_id=" in created.headers["Location"]
    with app.extensions["db_session_factory"]() as db:
        group_id = db.execute(select(ConsolidationGroup.id)).scalar_one()

    added = client.post(
        f"/konsolidierung/{group_id}/mitglieder",
        data={
            "company_id": world["child"],
            "ownership_percent": "75",
            "included_from": "",
            "included_to": "",
            "date_to": "2026-12-31",
        },
    )
    assert added.status_code == 302
    assert "date_to=2026-12-31" in added.headers["Location"]

    page = client.get(f"/konsolidierung?group_id={group_id}&date_to=2026-12-31")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Tochter GmbH" in html
    assert "Aufrechnungsdifferenz (Schulden)" in html
    assert "Bilanzgleichheit" in html
    assert "kein Konzernabschluss" in html
    assert "Fremdanteil 25,00 %" in html
    assert "consolidation-report.csv" in html

    rejected = client.post(
        f"/konsolidierung/{group_id}/buchungen",
        data={
            "adjustment_date": "2026-06-30",
            "kind": "other",
            "description": "Schief",
            "line_account_code": ["1400", "1600", ""],
            "line_debit_amount": ["5", "", ""],
            "line_credit_amount": ["", "4", ""],
        },
        follow_redirects=True,
    )
    assert "nicht ausgeglichen" in rejected.get_data(as_text=True)
    booked = client.post(
        f"/konsolidierung/{group_id}/buchungen",
        data={
            "adjustment_date": "2026-06-30",
            "kind": "capital",
            "description": "Testbuchung Kapital",
            "line_account_code": ["1400", "1600", ""],
            "line_debit_amount": ["5", "", ""],
            "line_credit_amount": ["", "5", ""],
            "date_to": "2026-12-31",
        },
        follow_redirects=True,
    )
    assert "Testbuchung Kapital" in booked.get_data(as_text=True)
    with app.extensions["db_session_factory"]() as db:
        group = db.get(ConsolidationGroup, group_id)
        adjustment_id = group.adjustments[0].id

    # Lesende Rollen sehen den Bericht, aber weder Formulare noch Schreibrechte.
    auditor = _login(app, "pruefer-a")
    html = auditor.get(f"/konsolidierung?group_id={group_id}").get_data(as_text=True)
    assert "Testbuchung Kapital" in html
    assert "Buchung erfassen" not in html
    assert "Neue Gruppe anlegen" not in html
    refused = auditor.post(
        f"/konsolidierung/{group_id}/buchungen/{adjustment_id}/loeschen", follow_redirects=True
    )
    assert "nur Lesezugriff" in refused.get_data(as_text=True)
    with app.extensions["db_session_factory"]() as db:
        assert db.get(ConsolidationGroup, group_id).adjustments
    outsider = _login(app, "buchhalter-b")
    assert "Testgruppe A" not in outsider.get("/konsolidierung").get_data(as_text=True)
    assert outsider.post(
        f"/konsolidierung/{group_id}/buchungen/{adjustment_id}/loeschen"
    ).status_code == 404
    assert outsider.post(
        f"/konsolidierung/{group_id}", data={"name": "Gekapert"}
    ).status_code == 404

    # Gruppe bearbeiten, Mitglied entfernen, Buchung löschen.
    updated = client.post(
        f"/konsolidierung/{group_id}",
        data={"name": "Testgruppe B", "parent_company_id": world["parent"], "is_active": "1"},
        follow_redirects=True,
    )
    assert "Testgruppe B" in updated.get_data(as_text=True)
    deleted = client.post(
        f"/konsolidierung/{group_id}/buchungen/{adjustment_id}/loeschen",
        follow_redirects=True,
    )
    assert "Audit-Log" in deleted.get_data(as_text=True)
    removed = client.post(
        f"/konsolidierung/{group_id}/mitglieder/{world['child']}/entfernen",
        follow_redirects=True,
    )
    assert "Mitglied wurde entfernt" in removed.get_data(as_text=True)
    refused = client.post(
        f"/konsolidierung/{group_id}/mitglieder/{world['parent']}/entfernen",
        follow_redirects=True,
    )
    assert "Muttergesellschaft" in refused.get_data(as_text=True)


def test_ui_partner_form_sets_intercompany_company(tmp_path: Path) -> None:
    app = _app(tmp_path)
    world = _seed_api_world(app)
    client = _login(app, "buchhalter-a")

    page = client.get(f"/partner?company_id={world['parent']}")
    assert "Verbundene Gesellschaft" in page.get_data(as_text=True)
    created = client.post(
        "/partner",
        data={
            "company_id": world["parent"],
            "name": "Formularpartner",
            "is_customer": "1",
            "intercompany_company_id": world["child"],
        },
    )
    assert created.status_code == 302
    detail_url = created.headers["Location"]
    html = client.get(detail_url).get_data(as_text=True)
    assert "Tochter GmbH" in html
    assert "Konsolidierung" in html

    partner_id = int(detail_url.rsplit("/", 1)[-1])
    cleared = client.post(
        f"/partner/{partner_id}/update",
        data={
            "name": "Formularpartner",
            "is_customer": "1",
            "is_active": "true",
            "intercompany_company_id": "",
        },
    )
    assert cleared.status_code == 302
    with app.extensions["db_session_factory"]() as db:
        assert db.get(BusinessPartner, partner_id).intercompany_company_id is None


def test_mcp_tools_reach_the_api(tmp_path: Path) -> None:
    app = _app(tmp_path, API_REQUIRE_AUTH=True)
    world = _seed_api_world(app)
    with app.extensions["db_session_factory"]() as db:
        user = db.execute(select(User).where(User.username == "buchhalter-a")).scalar_one()
        api_user = {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "tenant_id": user.tenant_id,
        }
    server = MCPServer(http=InProcessApiClient(app, api_user=api_user, global_access=False))

    def call(tool: str, /, **arguments) -> dict:
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        result = response["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    group = call(
        "create_consolidation_group",
        name="MCP-Gruppe",
        parent_company_id=world["parent"],
        members=[{"company_id": world["child"]}],
    )
    assert len(group["members"]) == 2
    assert call("list_consolidation_groups")["groups"][0]["id"] == group["id"]
    assert call("update_consolidation_group", group_id=group["id"], name="MCP neu")["changed"]
    adjustment = call(
        "create_consolidation_adjustment",
        group_id=group["id"],
        adjustment_date="2026-06-30",
        kind="other",
        description="Via MCP",
        lines=[
            {"account_code": "1400", "debit_amount": "5"},
            {"account_code": "1600", "credit_amount": "5"},
        ],
    )
    report = call("get_consolidation_report", group_id=group["id"], date_to="2026-12-31")
    assert report["balance_sheet"]["totals"]["is_balanced"] is True
    assert report["adjustments"][0]["id"] == adjustment["id"]
    link = call("export_consolidation_report_csv", group_id=group["id"], download_link=True)
    assert link["file_name"].startswith("konsolidierung-gruppe-")
    assert call(
        "delete_consolidation_adjustment",
        group_id=group["id"],
        adjustment_id=adjustment["id"],
        reason="Test",
    )["deleted"]
    partner = call(
        "create_partner",
        company_id=world["parent"],
        name="Via MCP",
        is_customer=True,
        intercompany_company_id=world["child"],
    )
    assert partner["intercompany_company_id"] == world["child"]
    assert (
        call("update_partner", partner_id=partner["id"], intercompany_company_id=None)[
            "intercompany_company_id"
        ]
        is None
    )


def test_chat_treats_reports_as_read_and_changes_as_confirmed() -> None:
    for name in (
        "list_consolidation_groups",
        "get_consolidation_report",
        "export_consolidation_report_csv",
    ):
        assert not chat_tool_blocked(name)
        assert not chat_tool_requires_confirmation(name)
    for name in (
        "create_consolidation_group",
        "update_consolidation_group",
        "create_consolidation_adjustment",
        "delete_consolidation_adjustment",
    ):
        assert not chat_tool_blocked(name)
        assert chat_tool_requires_confirmation(name)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_roundtrip_keeps_partners(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    session_factory = create_session_factory(url)
    with session_factory() as db:
        tenant = _tenant(db)
        parent = _company(db, tenant, "Mutter GmbH")
        child = _company(db, tenant, "Tochter GmbH")
        partner = _partner_for(db, parent, child)
        entry = _sale(db, parent, partner, "100")
        partner_id = partner.id
        entry_id = entry.id
        db.commit()

    engine = create_engine(url)
    config = _alembic_config(engine)
    with engine.connect() as connection:
        head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    triggers = {
        row[0]
        for row in engine.connect().execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        )
    }

    command.downgrade(config, "20261103_0061-1")
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("business_partner")}
    assert "intercompany_company_id" not in columns
    assert "consolidation_group" not in inspector.get_table_names()
    assert "consolidation_adjustment_line" not in inspector.get_table_names()

    command.upgrade(config, "head")
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("business_partner")}
    assert "intercompany_company_id" in columns
    assert "consolidation_member" in inspector.get_table_names()
    foreign_keys = inspector.get_foreign_keys("business_partner")
    assert any(key["constrained_columns"] == ["intercompany_company_id"] for key in foreign_keys)
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head
        )
        assert connection.execute(
            text("SELECT name FROM business_partner WHERE id = :id"), {"id": partner_id}
        ).scalar_one()
        assert connection.execute(
            text("SELECT count(*) FROM journal_entry WHERE id = :id"), {"id": entry_id}
        ).scalar_one() == 1
        assert {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        } == triggers
