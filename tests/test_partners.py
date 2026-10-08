"""Geschäftspartner-Stammdaten, Sammelkonten und Partner an Buchungszeilen (Nebenbuch)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, _alembic_head_revision, create_session_factory
from app.services.account_chart_import import (
    import_account_chart_csv,
    import_bundled_account_chart,
)
from app.services.accounts import AccountUpdateError, update_account_master_data
from app.services.compliance_integrity import (
    calculate_journal_entry_content_hash,
    verify_compliance_integrity,
)
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entry,
    reverse_journal_entry,
)
from app.services.journal_templates import JournalTemplateError, book_template, create_template
from app.services.opening_balance import book_opening_balance, parse_balance_csv
from app.services.partners import (
    PartnerError,
    create_partner,
    duplicate_hints,
    set_partner_bank_details,
    update_partner,
)
from app.services.periods import close_fiscal_year
from domain.models import (
    Account,
    AuditLog,
    Base,
    BusinessPartner,
    Company,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    Tenant,
    User,
)

VALID_IBAN = "DE89 3704 0044 0532 0130 00"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session, name: str = "Partner GmbH") -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name=f"{name} Mandant")
    company = Company(tenant=tenant, name=name, currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts: dict[str, Account] = {}
    for code, account_name, account_type, subledger in (
        ("1200", "Bank", "asset", None),
        ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
        ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", "creditor"),
        ("8400", "Erlöse", "income", None),
        ("4200", "Miete", "expense", None),
        ("0860", "Gewinnvortrag vor Verwendung", "equity", None),
    ):
        account = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code=code,
            name=account_name,
            account_type=account_type,
            subledger=subledger,
        )
        session.add(account)
        accounts[code] = account
    session.commit()
    return company, accounts


def _sale(
    session: Session,
    company: Company,
    accounts: dict[str, Account],
    *,
    amount: str,
    entry_date: date = date(2026, 3, 1),
    partner_id: int | None = None,
    partner_number: str | None = None,
) -> JournalEntry:
    value = Decimal(amount)
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Ausgangsrechnung",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(
                    account_id=accounts["1400"].id,
                    debit_amount=value,
                    credit_amount=Decimal("0.00"),
                    partner_id=partner_id,
                    partner_number=partner_number,
                ),
                JournalLineInput(
                    account_id=accounts["8400"].id,
                    debit_amount=Decimal("0.00"),
                    credit_amount=value,
                ),
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Stammdaten
# ---------------------------------------------------------------------------


def test_partner_numbers_follow_role_ranges(session: Session) -> None:
    company, _ = _seed(session)

    first = create_partner(
        session=session, company=company, changed_by="t", name="A", is_customer=True
    )
    second = create_partner(
        session=session, company=company, changed_by="t", name="B", is_customer=True
    )
    supplier = create_partner(
        session=session, company=company, changed_by="t", name="C", is_supplier=True
    )
    both = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="D",
        is_customer=True,
        is_supplier=True,
    )
    explicit = create_partner(
        session=session, company=company, changed_by="t", name="E", debtor_number="10500"
    )
    after_explicit = create_partner(
        session=session, company=company, changed_by="t", name="F", is_customer=True
    )

    assert (first.debtor_number, first.creditor_number) == ("10000", None)
    assert second.debtor_number == "10001"
    assert (supplier.debtor_number, supplier.creditor_number) == (None, "70000")
    assert (both.debtor_number, both.creditor_number) == ("10002", "70001")
    assert explicit.debtor_number == "10500"
    assert after_explicit.debtor_number == "10501"

    with pytest.raises(PartnerError, match="10000–69999"):
        create_partner(
            session=session, company=company, changed_by="t", name="G", debtor_number="70000"
        )
    with pytest.raises(PartnerError, match="mindestens eine Rolle"):
        create_partner(session=session, company=company, changed_by="t", name="H")
    with pytest.raises(PartnerError, match="bereits vergeben"):
        create_partner(
            session=session, company=company, changed_by="t", name="I", debtor_number="10000"
        )
    with pytest.raises(PartnerError, match="Name ist Pflicht"):
        create_partner(session=session, company=company, changed_by="t", is_customer=True)


def test_partner_fields_are_normalized_and_validated(session: Session) -> None:
    company, _ = _seed(session)
    partner = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="  Muster   GmbH ",
        is_customer=True,
        vat_id="de 123 456 789",
        country_code="at",
        payment_term_days="30",
        email="info@muster.example",
    )
    assert partner.name == "Muster GmbH"
    assert partner.vat_id == "DE123456789"
    assert partner.country_code == "AT"
    assert partner.payment_term_days == 30

    for field, value, message in (
        ("vat_id", "DE12", "USt-IdNr"),
        ("email", "kein-mail", "E-Mail"),
        ("payment_term_days", -1, "zwischen 0 und 365"),
        ("country_code", "DEU", "ISO-Ländercode"),
        ("partner_kind", "verein", "organization oder person"),
    ):
        with pytest.raises(PartnerError, match=message):
            update_partner(session=session, partner=partner, changed_by="t", **{field: value})
        session.rollback()

    with pytest.raises(PartnerError, match="Bankdaten"):
        update_partner(session=session, partner=partner, changed_by="t", iban=VALID_IBAN)
    with pytest.raises(PartnerError, match="IBAN"):
        set_partner_bank_details(session=session, partner=partner, changed_by="t", iban="DE00 1")
    session.rollback()


def test_partner_history_records_changes_and_bank_details(session: Session) -> None:
    company, _ = _seed(session)
    partner = create_partner(
        session=session, company=company, changed_by="anna", name="Lieferant X", is_supplier=True
    )
    assert update_partner(session=session, partner=partner, changed_by="anna", city="Köln")
    assert not update_partner(session=session, partner=partner, changed_by="anna", city="Köln")
    assert set_partner_bank_details(
        session=session, partner=partner, changed_by="ben", iban=VALID_IBAN, bic="COBADEFFXXX"
    )
    session.commit()

    events = (
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "business_partner",
                AuditLog.entity_id == str(partner.id),
            )
            .order_by(AuditLog.id)
        )
        .scalars()
        .all()
    )
    assert [event.action for event in events] == ["created", "updated", "bank_details_changed"]
    assert events[0].payload["before"] is None
    assert events[1].payload["before"]["city"] is None
    assert events[1].payload["after"]["city"] == "Köln"
    assert events[2].payload["after"]["iban"] == "DE89370400440532013000"
    assert events[2].changed_by == "ben"

    other = create_partner(
        session=session, company=company, changed_by="t", name="lieferant  x", is_supplier=True
    )
    hints = duplicate_hints(
        session=session,
        company_id=company.id,
        name=other.name,
        iban="DE89370400440532013000",
        exclude_id=other.id,
    )
    assert any("IBAN" in hint for hint in hints)
    assert any("Namen" in hint for hint in hints)


# ---------------------------------------------------------------------------
# Nebenbuch an Buchungszeilen
# ---------------------------------------------------------------------------


def test_partner_only_on_matching_collective_account(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    supplier = create_partner(
        session=session, company=company, changed_by="t", name="Lieferant", is_supplier=True
    )
    session.commit()

    entry = _sale(session, company, accounts, amount="119.00", partner_id=customer.id)
    by_account = {line.account_id: line for line in entry.lines}
    assert by_account[accounts["1400"].id].partner_id == customer.id
    assert by_account[accounts["8400"].id].partner_id is None

    by_number = _sale(session, company, accounts, amount="10.00", partner_number="10000")
    assert {line.partner_id for line in by_number.lines} == {customer.id, None}

    with pytest.raises(JournalEntryCreationError, match="kein Kunde"):
        _sale(session, company, accounts, amount="5.00", partner_id=supplier.id)
    session.rollback()
    with pytest.raises(JournalEntryCreationError, match="nicht gefunden"):
        _sale(session, company, accounts, amount="5.00", partner_number="19999")
    session.rollback()

    with pytest.raises(JournalEntryCreationError, match="kein Debitoren-/Kreditoren-Sammelkonto"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 3, 1),
                description="Partner auf Erlöskonto",
                status="posted",
                changed_by="t",
                lines=[
                    JournalLineInput(accounts["1200"].id, Decimal("5.00"), Decimal("0.00")),
                    JournalLineInput(
                        accounts["8400"].id,
                        Decimal("0.00"),
                        Decimal("5.00"),
                        partner_id=customer.id,
                    ),
                ],
            ),
        )
    session.rollback()

    other_company, _ = _seed(session, name="Fremd GmbH")
    foreign = create_partner(
        session=session, company=other_company, changed_by="t", name="Fremd", is_customer=True
    )
    session.commit()
    with pytest.raises(JournalEntryCreationError, match="nicht gefunden"):
        _sale(session, company, accounts, amount="5.00", partner_id=foreign.id)
    session.rollback()

    update_partner(session=session, partner=customer, changed_by="t", is_active=False)
    session.commit()
    with pytest.raises(JournalEntryCreationError, match="inaktiv"):
        _sale(session, company, accounts, amount="5.00", partner_id=customer.id)
    session.rollback()


def test_storno_mirrors_partner_and_seal_version_3_covers_it(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    entry = _sale(session, company, accounts, amount="119.00", partner_id=customer.id)

    finalized = finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
    assert finalized.content_hash_version == 3
    assert finalized.content_hash == calculate_journal_entry_content_hash(finalized)

    # Der Partner ist Teil des versiegelten Inhalts.
    receivable_line = next(line for line in finalized.lines if line.partner_id is not None)
    receivable_line.partner_id = None
    assert calculate_journal_entry_content_hash(finalized) != finalized.content_hash
    session.expire_all()

    # Auch ein inzwischen deaktivierter Partner wird beim Storno gespiegelt.
    update_partner(session=session, partner=customer, changed_by="t", is_active=False)
    session.commit()
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 3, 2),
        changed_by="t",
    )
    mirrored = {line.account_id: line for line in reversal.lines}
    assert mirrored[accounts["1400"].id].partner_id == customer.id
    assert mirrored[accounts["1400"].id].credit_amount == Decimal("119.00")
    assert reversal.content_hash_version == 3
    assert verify_compliance_integrity(session=session, company_id=company.id).valid is True


def test_version_2_seals_remain_verifiable(session: Session) -> None:
    company, accounts = _seed(session)
    entry = _sale(session, company, accounts, amount="50.00")
    finalized = finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
    version_3_hash = finalized.content_hash

    # Bestandssiegel vor Einführung des Nebenbuchs (ohne DB-Trigger im Testschema).
    finalized.content_hash_version = 2
    finalized.content_hash = calculate_journal_entry_content_hash(finalized)
    session.commit()

    assert finalized.content_hash != version_3_hash
    result = verify_compliance_integrity(session=session, company_id=company.id)
    assert result.valid is True
    assert result.finalized_entries_checked == 1


def test_roles_and_collective_flag_are_fixed_after_use(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    _sale(session, company, accounts, amount="20.00", partner_id=customer.id)

    with pytest.raises(PartnerError, match="bereits in Buchungen"):
        update_partner(session=session, partner=customer, changed_by="t", is_customer=False)
    session.rollback()
    with pytest.raises(PartnerError, match="unveränderlich"):
        update_partner(session=session, partner=customer, changed_by="t", debtor_number="10100")
    session.rollback()

    # Eine unbenutzte Rolle lässt sich ergänzen und wieder entfernen.
    assert update_partner(session=session, partner=customer, changed_by="t", is_supplier=True)
    assert customer.creditor_number == "70000"
    assert update_partner(session=session, partner=customer, changed_by="t", is_supplier=False)
    assert customer.creditor_number is None
    session.commit()

    with pytest.raises(AccountUpdateError, match="Geschäftspartner"):
        update_account_master_data(
            session=session, account=accounts["1400"], changed_by="t", subledger=None
        )


def test_unused_role_can_be_swapped_in_one_update(session: Session) -> None:
    company, _ = _seed(session)
    partner = create_partner(
        session=session, company=company, changed_by="t", name="Falsch erfasst", is_customer=True
    )
    session.commit()

    assert update_partner(
        session=session, partner=partner, changed_by="t", is_customer=False, is_supplier=True
    )
    session.commit()
    assert (partner.debtor_number, partner.creditor_number) == (None, "70000")

    with pytest.raises(PartnerError, match="mindestens eine Rolle"):
        update_partner(session=session, partner=partner, changed_by="t", is_supplier=False)
    session.rollback()


def test_collective_flag_and_type_repair_rules(session: Session) -> None:
    company, accounts = _seed(session)

    with pytest.raises(AccountUpdateError, match="Kontoart asset"):
        update_account_master_data(
            session=session, account=accounts["8400"], changed_by="t", subledger="debtor"
        )
    with pytest.raises(AccountUpdateError, match="Kontoart liability"):
        update_account_master_data(
            session=session, account=accounts["1200"], changed_by="t", subledger="creditor"
        )
    with pytest.raises(AccountUpdateError, match="unveränderbar"):
        update_account_master_data(
            session=session, account=accounts["1200"], changed_by="t", account_type="expense"
        )

    broken = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="4240",
        name="Gas",
        account_type="Strom",
    )
    session.add(broken)
    session.commit()
    assert update_account_master_data(
        session=session,
        account=broken,
        changed_by="t",
        name="Gas, Strom, Wasser",
        account_type="expense",
    )
    session.commit()
    event = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "account",
            AuditLog.entity_id == str(broken.id),
        )
    ).scalar_one()
    assert event.payload["before"]["account_type"] == "Strom"
    assert event.payload["after"]["account_type"] == "expense"


def test_year_end_carryforward_keeps_partner_balances(session: Session) -> None:
    company, accounts = _seed(session)
    first = create_partner(
        session=session, company=company, changed_by="t", name="Kunde A", is_customer=True
    )
    second = create_partner(
        session=session, company=company, changed_by="t", name="Kunde B", is_customer=True
    )
    session.commit()
    _sale(session, company, accounts, amount="119.00", partner_id=first.id)
    _sale(session, company, accounts, amount="238.00", partner_id=second.id)
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 1),
            description="Teilzahlung Kunde B",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(accounts["1200"].id, Decimal("100.00"), Decimal("0.00")),
                JournalLineInput(
                    accounts["1400"].id,
                    Decimal("0.00"),
                    Decimal("100.00"),
                    partner_id=second.id,
                ),
            ],
        ),
    )
    # Inzwischen deaktivierte Partner werden trotzdem vorgetragen.
    update_partner(session=session, partner=first, changed_by="t", is_active=False)
    session.commit()

    fiscal_year = session.execute(select(FiscalYear)).scalar_one()
    result = close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="t")
    opening_lines = (
        session.execute(
            select(JournalEntryLine).where(
                JournalEntryLine.journal_entry_id == result.opening_balance_entry.id,
                JournalEntryLine.account_id == accounts["1400"].id,
            )
        )
        .scalars()
        .all()
    )
    assert {line.partner_id: line.debit_amount for line in opening_lines} == {
        first.id: Decimal("119.00"),
        second.id: Decimal("138.00"),
    }


def test_opening_balance_and_templates_carry_partner(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()

    balances = parse_balance_csv("Konto;Soll;Haben;Partner\n1400;500,00;;10000\n0860;;500,00\n")
    assert balances[0]["partner_number"] == "10000"
    entry = book_opening_balance(
        session=session,
        company_id=company.id,
        entry_date=date(2026, 1, 1),
        balances=balances,
        changed_by="t",
    )
    assert {line.account_id: line.partner_id for line in entry.lines}[
        accounts["1400"].id
    ] == customer.id

    template = create_template(
        session=session,
        company_id=company.id,
        name="Wartungsvertrag",
        description="Wartung monatlich",
        lines=[
            {"account_id": accounts["1400"].id, "debit": "50.00", "partner_id": customer.id},
            {"account_id": accounts["8400"].id, "credit": "50.00"},
        ],
        changed_by="t",
    )
    session.commit()
    booked, _ = book_template(session=session, template_id=template.id, changed_by="t")
    assert customer.id in {line.partner_id for line in booked.lines}

    with pytest.raises(JournalTemplateError, match="Sammelkonten"):
        create_template(
            session=session,
            company_id=company.id,
            name="Falsch",
            description="Partner auf Erlöskonto",
            lines=[
                {"account_id": accounts["1200"].id, "debit": "50.00"},
                {"account_id": accounts["8400"].id, "credit": "50.00", "partner_id": customer.id},
            ],
            changed_by="t",
        )


def test_chart_import_detects_collective_accounts_and_rejects_bad_rows(session: Session) -> None:
    tenant = Tenant(name="Import")
    company = Company(tenant=tenant, name="Import GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.commit()

    report = import_bundled_account_chart(session=session, company_id=company.id, chart="skr03")
    assert report.error_rows == 0
    flags = {
        account.code: account.subledger
        for account in session.execute(
            select(Account).where(Account.company_id == company.id)
        ).scalars()
    }
    assert flags["1400"] == "debtor"
    assert flags["1600"] == "creditor"
    assert {code for code, flag in flags.items() if flag} == {"1400", "1600"}

    report = import_account_chart_csv(
        session=session,
        company_id=company.id,
        csv_stream=io.StringIO(
            "code,name,account_type,subledger\n"
            "1410,Forderungen verbundene Unternehmen,asset,debtor\n"
            "4240,Gas, Strom, Wasser,expense\n"
            "4250,Reinigung,expenses,\n"
            "1610,Sonstige Verbindlichkeiten,liability,debtor\n"
        ),
    )
    assert report.imported_rows == 1
    assert report.error_rows == 3
    messages = " ".join(error.message for error in report.errors)
    assert "Anführungszeichen" in messages
    assert "Unbekannter Kontotyp" in messages
    assert "Kontoart asset" in messages


# ---------------------------------------------------------------------------
# REST-API, MCP-Parität, Oberfläche, Exporte
# ---------------------------------------------------------------------------


def _app(tmp_path: Path, **config):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'partners.db'}",
            **config,
        }
    )


def _admin_client(app):
    with app.extensions["db_session_factory"]() as db:
        db.add(
            User(
                username="admin",
                password_hash=hash_password("geheim123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db.commit()
    client = app.test_client()
    response = client.post("/auth/login", data={"username": "admin", "password": "geheim123"})
    assert response.status_code == 302
    return client


def _seed_company_via_api(client) -> int:
    tenant = client.post(
        "/api/v1/tenants", json={"tenant_name": "Partner", "company_name": "Partner GmbH"}
    )
    assert tenant.status_code == 201
    company_id = tenant.get_json()["company"]["id"]
    for code, name, account_type, subledger in (
        ("1200", "Bank", "asset", None),
        ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
        ("8400", "Erlöse", "income", None),
    ):
        response = client.post(
            "/api/v1/accounts",
            json={
                "company_id": company_id,
                "code": code,
                "name": name,
                "account_type": account_type,
                "subledger": subledger,
            },
        )
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["subledger"] == subledger
    return company_id


def test_partner_api_journal_exports_and_ui(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    company_id = _seed_company_via_api(client)

    invalid_type = client.post(
        "/api/v1/accounts",
        json={"company_id": company_id, "code": "1500", "name": "X", "account_type": "receivable"},
    )
    assert invalid_type.status_code == 400

    with_bank = client.post(
        "/api/v1/partners",
        json={"company_id": company_id, "name": "Kunde", "is_customer": True, "iban": VALID_IBAN},
    )
    assert with_bank.status_code == 400
    assert "bank-details" in with_bank.get_json()["error"]

    created = client.post(
        "/api/v1/partners",
        json={
            "company_id": company_id,
            "name": "Kunde Nord GmbH",
            "is_customer": True,
            "city": "Hamburg",
            "vat_id": "DE123456789",
            "payment_term_days": 14,
        },
    )
    assert created.status_code == 201, created.get_json()
    partner = created.get_json()
    assert partner["debtor_number"] == "10000"
    assert partner["is_customer"] is True and partner["is_supplier"] is False
    partner_id = partner["id"]

    duplicate = client.post(
        "/api/v1/partners",
        json={"company_id": company_id, "name": "Kunde Nord GmbH", "is_supplier": True},
    )
    assert duplicate.status_code == 201
    assert duplicate.get_json()["creditor_number"] == "70000"
    assert duplicate.get_json()["warnings"]

    listed = client.get(
        "/api/v1/partners", query_string={"company_id": company_id, "role": "debtor"}
    ).get_json()
    assert listed["total"] == 1 and listed["partners"][0]["id"] == partner_id
    searched = client.get(
        "/api/v1/partners", query_string={"company_id": company_id, "q": "70000"}
    ).get_json()
    assert [item["creditor_number"] for item in searched["partners"]] == ["70000"]

    patched = client.patch(f"/api/v1/partners/{partner_id}", json={"street": "Hafenstraße 1"})
    assert patched.status_code == 200 and patched.get_json()["changed"] is True
    assert client.patch(
        f"/api/v1/partners/{partner_id}", json={"iban": VALID_IBAN}
    ).status_code == 400
    bank = client.post(
        f"/api/v1/partners/{partner_id}/bank-details", json={"iban": VALID_IBAN}
    )
    assert bank.status_code == 200 and bank.get_json()["iban"] == "DE89370400440532013000"
    history = client.get(f"/api/v1/partners/{partner_id}/history").get_json()
    assert [entry["action"] for entry in history["entries"]] == [
        "bank_details_changed",
        "updated",
        "created",
    ]
    assert client.get(f"/api/v1/partners/{partner_id}").get_json()["street"] == "Hafenstraße 1"

    booking = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-05-04",
            "description": "AR 2026-17",
            "lines": [
                {"account_code": "1400", "debit_amount": "119.00", "partner_number": "10000"},
                {"account_code": "8400", "credit_amount": "119.00"},
            ],
        },
    )
    assert booking.status_code == 201, booking.get_json()
    rejected = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-05-04",
            "description": "Partner auf Erlöskonto",
            "lines": [
                {"account_code": "1200", "debit_amount": "1.00"},
                {"account_code": "8400", "credit_amount": "1.00", "partner_id": partner_id},
            ],
        },
    )
    assert rejected.status_code == 422

    entries = client.get(
        "/api/v1/journal-entries", query_string={"company_id": company_id}
    ).get_json()["entries"]
    receivable_line = next(
        line for line in entries[0]["lines"] if line["account_code"] == "1400"
    )
    assert receivable_line["partner_id"] == partner_id
    assert receivable_line["partner_number"] == "10000"
    assert receivable_line["partner_name"] == "Kunde Nord GmbH"

    journal_csv = client.get(
        "/api/v1/exports/journal.csv", query_string={"company_id": company_id}
    ).get_data(as_text=True)
    header, *rows = journal_csv.strip().splitlines()
    assert header.endswith("partner_number,partner_name")
    assert any(row.endswith("10000,Kunde Nord GmbH") for row in rows)

    package = client.get(
        "/api/v1/exports/audit-package.zip",
        query_string={"company_id": company_id, "include_documents": "false"},
    )
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        partners_json = json.loads(archive.read("data/business_partners.json"))
        history_json = json.loads(archive.read("data/business_partner_history.json"))
        catalog = json.loads(archive.read("schema/field_catalog.json"))
    assert {item["name"] for item in partners_json} == {"Kunde Nord GmbH"}
    assert len(history_json) == 4
    assert "business_partners" in json.dumps(catalog)

    # Oberfläche: Liste, Anlage per Formular, Detail, Buchungsmaske, Konten.
    page = client.get(f"/partner?company_id={company_id}")
    assert page.status_code == 200
    assert "Kunde Nord GmbH" in page.get_data(as_text=True)
    form = client.post(
        "/partner",
        data={
            "company_id": str(company_id),
            "name": "Vermieter KG",
            "is_supplier": "1",
            "iban": VALID_IBAN,
            "payment_term_days": "0",
        },
    )
    assert form.status_code == 302
    detail = client.get(form.headers["Location"])
    detail_text = detail.get_data(as_text=True)
    assert "Vermieter KG" in detail_text and "Kreditor 70001" in detail_text
    assert "bank_details_changed" in detail_text
    journal_page = client.get(f"/buchungen?company_id={company_id}").get_data(as_text=True)
    assert 'name="line_partner_id"' in journal_page
    assert 'data-subledger="debtor"' in journal_page
    assert "Partner 10000 Kunde Nord GmbH" in journal_page
    accounts_page = client.get(f"/konten?company_id={company_id}").get_data(as_text=True)
    assert "Debitoren-Sammelkonto" in accounts_page
    assert 'value="receivable"' not in accounts_page


def test_partner_api_is_tenant_scoped_and_read_only_for_pruefer(tmp_path: Path) -> None:
    app = _app(tmp_path, API_REQUIRE_AUTH=True)
    with app.extensions["db_session_factory"]() as db:
        tenant_a = Tenant(name="A")
        tenant_b = Tenant(name="B")
        company_a = Company(name="A GmbH", currency_code="EUR", tenant=tenant_a)
        company_b = Company(name="B GmbH", currency_code="EUR", tenant=tenant_b)
        db.add_all([tenant_a, tenant_b, company_a, company_b])
        db.flush()
        foreign = BusinessPartner(
            tenant_id=tenant_b.id,
            company_id=company_b.id,
            name="Fremdkunde",
            debtor_number="10000",
        )
        db.add(foreign)
        for username, role, token in (
            ("buchhalter-a", "Buchhalter", "obk_buchhalter-a"),
            ("pruefer-a", "Pruefer", "obk_pruefer-a"),
        ):
            db.add(
                User(
                    username=username,
                    password_hash=hash_password("passwort"),
                    role=role,
                    tenant_id=tenant_a.id,
                    api_token_hash=hash_api_token(token),
                    api_token_last4=token[-4:],
                )
            )
        db.commit()
        company_a_id, company_b_id, foreign_id = company_a.id, company_b.id, foreign.id

    client = app.test_client()
    writer = {"Authorization": "Bearer obk_buchhalter-a"}
    reader = {"Authorization": "Bearer obk_pruefer-a"}

    own = client.post(
        "/api/v1/partners",
        headers=writer,
        json={"company_id": company_a_id, "name": "Eigenkunde", "is_customer": True},
    )
    assert own.status_code == 201
    assert client.get(f"/api/v1/partners/{foreign_id}", headers=writer).status_code == 404
    assert client.patch(
        f"/api/v1/partners/{foreign_id}", headers=writer, json={"name": "X"}
    ).status_code == 404
    assert client.get(
        "/api/v1/partners", headers=writer, query_string={"company_id": company_b_id}
    ).status_code == 404

    assert client.get(
        "/api/v1/partners", headers=reader, query_string={"company_id": company_a_id}
    ).get_json()["total"] == 1
    assert client.post(
        "/api/v1/partners",
        headers=reader,
        json={"company_id": company_a_id, "name": "Nicht erlaubt", "is_customer": True},
    ).status_code == 403
    assert client.post(
        f"/api/v1/partners/{own.get_json()['id']}/bank-details",
        headers=reader,
        json={"iban": VALID_IBAN},
    ).status_code == 403


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _trigger_names(engine) -> set[str]:
    with engine.connect() as connection:
        return {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }


def test_partner_migration_roundtrip_keeps_seals_valid(tmp_path: Path) -> None:
    db_path = tmp_path / "partner-roundtrip.db"
    url = f"sqlite+pysqlite:///{db_path}"
    session_factory = create_session_factory(url)
    with session_factory() as session:
        company, accounts = _seed(session)
        customer = create_partner(
            session=session, company=company, changed_by="t", name="Kunde", is_customer=True
        )
        session.commit()
        entry = _sale(session, company, accounts, amount="119.00", partner_id=customer.id)
        finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
        entry_id = entry.id
        company_id = company.id

    engine = create_engine(url)
    config = _alembic_config(engine)
    triggers_at_head = _trigger_names(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT content_hash_version FROM journal_entry WHERE id = :id"),
            {"id": entry_id},
        ).scalar_one() == 3

    command.downgrade(config, "20261008_0041")
    columns = {column["name"] for column in inspect(engine).get_columns("journal_entry_line")}
    assert "partner_id" not in columns
    assert "business_partner" not in inspect(engine).get_table_names()
    assert _trigger_names(engine) == triggers_at_head
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT content_hash_version FROM journal_entry WHERE id = :id"),
            {"id": entry_id},
        ).scalar_one() == 2

    command.upgrade(config, "head")
    assert _trigger_names(engine) == triggers_at_head
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert row == _alembic_head_revision()
    with Session(engine) as session:
        assert verify_compliance_integrity(session=session, company_id=company_id).valid is True
        # Festgeschriebene Zeilen bleiben unveränderbar – auch die neue Spalte.
        with pytest.raises(Exception, match="immutable"):
            session.execute(
                text(
                    "UPDATE journal_entry_line SET partner_id = NULL "
                    "WHERE journal_entry_id = :id"
                ),
                {"id": entry_id},
            )
