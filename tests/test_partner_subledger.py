"""Partner am offenen Posten, im Zahllauf und Mahnwesen; Partnerkonto und Saldenliste."""

from __future__ import annotations

import base64
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services.dunning import dunning_proposals, dunning_recipient_lines, serialize_proposal
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    list_open_items,
    set_open_item_partner,
    settle_open_item,
)
from app.services.partner_ledger import PartnerLedgerError, partner_balances, partner_statement
from app.services.partners import create_partner, set_partner_bank_details
from app.services.periods import close_fiscal_year
from app.services.sepa_export import (
    PAIN_NAMESPACE,
    create_payment_run,
    payable_items_for_run,
    payment_recipient,
)
from domain.models import Account, AuditLog, Base, Company, FiscalYear, Tenant, User

PARTNER_IBAN = "DE89370400440532013000"
OTHER_IBAN = "DE02120300000000202051"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session, name: str = "Nebenbuch GmbH") -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name=f"{name} Mandant")
    company = Company(tenant=tenant, name=name, currency_code="EUR", iban=PARTNER_IBAN)
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


def _customer(session, company, name="Kunde AG", **fields):
    partner = create_partner(
        session=session, company=company, changed_by="t", name=name, is_customer=True, **fields
    )
    session.commit()
    return partner


def _supplier(session, company, name="Lieferant KG", iban=None, bic=None, **fields):
    partner = create_partner(
        session=session, company=company, changed_by="t", name=name, is_supplier=True, **fields
    )
    if iban:
        set_partner_bank_details(
            session=session, partner=partner, changed_by="t", iban=iban, bic=bic or ""
        )
    session.commit()
    return partner


def _item(session, company, account, **overrides):
    values = {
        "company_id": company.id,
        "account_id": account.id,
        "item_type": "receivable",
        "reference": "RE-1",
        "entry_date": date.today() - timedelta(days=30),
        "amount": Decimal("119.00"),
        "changed_by": "t",
    }
    values.update(overrides)
    return create_open_item(session=session, payload=OpenItemInput(**values))


def _book(session, company, debit, credit, amount, *, entry_date, partner_id=None, partner_on=None):
    value = Decimal(amount)
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Buchung",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(
                    account_id=debit.id,
                    debit_amount=value,
                    credit_amount=Decimal("0.00"),
                    partner_id=partner_id if partner_on == "debit" else None,
                ),
                JournalLineInput(
                    account_id=credit.id,
                    debit_amount=Decimal("0.00"),
                    credit_amount=value,
                    partner_id=partner_id if partner_on == "credit" else None,
                ),
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Offene Posten
# ---------------------------------------------------------------------------


def test_open_item_takes_snapshot_from_partner(session: Session) -> None:
    company, accounts = _seed(session)
    supplier = _supplier(
        session, company, iban=PARTNER_IBAN, bic="COBADEFFXXX", payment_term_days=14
    )
    item = _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        partner_id=supplier.id,
        entry_date=date(2026, 3, 1),
    )
    assert item.partner_id == supplier.id
    assert item.counterparty == "Lieferant KG"
    assert item.counterparty_iban == PARTNER_IBAN
    assert item.counterparty_bic == "COBADEFFXXX"
    assert item.due_date == date(2026, 3, 15)

    # Erfasste Angaben gehen dem Stamm vor.
    explicit = _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        reference="ER-2",
        partner_id=supplier.id,
        counterparty="Abweichender Name",
        counterparty_iban=OTHER_IBAN,
        entry_date=date(2026, 3, 1),
        due_date=date(2026, 4, 1),
    )
    assert explicit.counterparty == "Abweichender Name"
    assert explicit.counterparty_iban == OTHER_IBAN
    assert explicit.counterparty_bic is None
    assert explicit.due_date == date(2026, 4, 1)


def test_open_item_partner_must_fit_role_account_and_status(session: Session) -> None:
    company, accounts = _seed(session)
    other_company, _ = _seed(session, "Fremd GmbH")
    customer = _customer(session, company)
    supplier = _supplier(session, company)
    foreign = _customer(session, other_company, "Fremdkunde")

    with pytest.raises(OpenItemError, match="kein Kunde"):
        _item(session, company, accounts["1400"], partner_id=supplier.id)
    with pytest.raises(OpenItemError, match="kein Lieferant"):
        _item(session, company, accounts["1600"], item_type="payable", partner_id=customer.id)
    with pytest.raises(OpenItemError, match="kein Debitoren-Sammelkonto"):
        _item(session, company, accounts["1200"], partner_id=customer.id)
    with pytest.raises(OpenItemError, match="nicht gefunden"):
        _item(session, company, accounts["1400"], partner_id=foreign.id)

    customer.is_active = False
    session.commit()
    with pytest.raises(OpenItemError, match="inaktiv"):
        _item(session, company, accounts["1400"], partner_id=customer.id)


def test_assign_partner_to_existing_item_keeps_free_text(session: Session) -> None:
    company, accounts = _seed(session)
    supplier = _supplier(session, company, iban=PARTNER_IBAN)
    item = _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        counterparty="Lieferant (alt)",
    )
    updated = set_open_item_partner(
        session=session, open_item_id=item.id, partner_id=supplier.id, changed_by="t"
    )
    assert updated.partner_id == supplier.id
    assert updated.counterparty == "Lieferant (alt)"
    assert updated.counterparty_iban == PARTNER_IBAN

    # Gleicher Partner erzeugt keinen zweiten Audit-Eintrag; entfernen ist möglich.
    set_open_item_partner(
        session=session, open_item_id=item.id, partner_id=supplier.id, changed_by="t"
    )
    removed = set_open_item_partner(
        session=session, open_item_id=item.id, partner_id=None, changed_by="t"
    )
    assert removed.partner_id is None
    actions = session.execute(
        select(AuditLog.action).where(
            AuditLog.entity_type == "open_item", AuditLog.entity_id == str(item.id)
        )
    ).scalars()
    assert list(actions).count("partner_assigned") == 2


def test_list_filters_by_partner(session: Session) -> None:
    company, accounts = _seed(session)
    first = _customer(session, company, "Erster")
    second = _customer(session, company, "Zweiter")
    _item(session, company, accounts["1400"], reference="A", partner_id=first.id)
    _item(session, company, accounts["1400"], reference="B", partner_id=second.id)
    _item(session, company, accounts["1400"], reference="C")
    items = list_open_items(session=session, company_id=company.id, partner_id=second.id)
    assert [item.reference for item in items] == ["B"]


def test_settlement_rejects_payment_booked_on_other_partner(session: Session) -> None:
    company, accounts = _seed(session)
    first = _customer(session, company, "Erster")
    second = _customer(session, company, "Zweiter")
    item = _item(session, company, accounts["1400"], partner_id=first.id)
    payment = _book(
        session,
        company,
        accounts["1200"],
        accounts["1400"],
        "119.00",
        entry_date=date(2026, 3, 5),
        partner_id=second.id,
        partner_on="credit",
    )
    with pytest.raises(OpenItemError, match="anderen Geschäftspartner"):
        settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )

    # Zahlung ohne Partner oder mit passendem Partner ist zulässig.
    neutral = _book(
        session,
        company,
        accounts["1200"],
        accounts["1400"],
        "19.00",
        entry_date=date(2026, 3, 6),
    )
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=neutral.id,
        amount=Decimal("19.00"),
    )
    assert settled.open_amount == Decimal("100.00")
    fitting = _book(
        session,
        company,
        accounts["1200"],
        accounts["1400"],
        "100.00",
        entry_date=date(2026, 3, 7),
        partner_id=first.id,
        partner_on="credit",
    )
    settled = settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=fitting.id
    )
    assert settled.status == "settled"


# ---------------------------------------------------------------------------
# Zahllauf
# ---------------------------------------------------------------------------


def _pain_recipients(xml_bytes: bytes) -> list[tuple[str, str]]:
    ns = {"p": PAIN_NAMESPACE}
    root = ET.fromstring(xml_bytes)
    return [
        (
            tx.findtext("p:Cdtr/p:Nm", namespaces=ns),
            tx.findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=ns),
        )
        for tx in root.iterfind(".//p:CdtTrfTxInf", ns)
    ]


def test_payment_run_uses_partner_iban_and_warns_on_mismatch(session: Session) -> None:
    company, accounts = _seed(session)
    supplier = _supplier(session, company, iban=PARTNER_IBAN)
    # Posten vor der Bankverbindung angelegt: Fallback auf den Partner.
    bare_supplier = _supplier(session, company, "Spät KG")
    fallback = _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        reference="ER-1",
        partner_id=bare_supplier.id,
    )
    assert fallback.counterparty_iban is None
    set_partner_bank_details(
        session=session, partner=bare_supplier, changed_by="t", iban=OTHER_IBAN, bic=""
    )
    session.commit()
    mismatch = _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        reference="ER-2",
        partner_id=supplier.id,
        counterparty_iban=OTHER_IBAN,
    )
    without_iban = _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        reference="ER-3",
    )

    proposals = {
        item.reference for item in payable_items_for_run(session=session, company_id=company.id)
    }
    assert proposals == {"ER-1", "ER-2"}
    assert without_iban.reference not in proposals
    assert payment_recipient(mismatch).iban_mismatch is True
    assert payment_recipient(fallback).iban_mismatch is False

    result = create_payment_run(
        session=session,
        company_id=company.id,
        open_item_ids=[fallback.id, mismatch.id],
        changed_by="t",
    )
    recipients = _pain_recipients(result.xml_bytes)
    assert ("Spät KG", OTHER_IBAN) in recipients
    assert ("Lieferant KG", OTHER_IBAN) in recipients
    assert len(result.warnings) == 1
    assert "ER-2" in result.warnings[0]


# ---------------------------------------------------------------------------
# Mahnwesen
# ---------------------------------------------------------------------------


def test_dunning_letter_address_comes_from_partner(session: Session) -> None:
    company, accounts = _seed(session)
    customer = _customer(
        session,
        company,
        street="Musterstr. 1",
        postal_code="12345",
        city="Musterstadt",
        contact_person="Frau Beispiel",
    )
    item = _item(
        session,
        company,
        accounts["1400"],
        partner_id=customer.id,
        due_date=date.today() - timedelta(days=10),
    )
    assert dunning_recipient_lines(item) == [
        "Kunde AG",
        "Frau Beispiel",
        "Musterstr. 1",
        "12345 Musterstadt",
    ]
    proposal = dunning_proposals(session=session, company_id=company.id)[0]
    payload = serialize_proposal(proposal)
    assert payload["partner_id"] == customer.id
    assert payload["partner_number"] == customer.debtor_number

    free_text = _item(
        session,
        company,
        accounts["1400"],
        reference="RE-2",
        counterparty="Einmalkunde",
        due_date=date.today() - timedelta(days=10),
    )
    assert dunning_recipient_lines(free_text) == ["Einmalkunde"]


# ---------------------------------------------------------------------------
# Partnerkonto und Saldenliste
# ---------------------------------------------------------------------------


def test_statement_has_opening_running_balance_and_only_collective_accounts(
    session: Session,
) -> None:
    company, accounts = _seed(session)
    customer = _customer(session, company)
    _book(
        session,
        company,
        accounts["1400"],
        accounts["8400"],
        "100.00",
        entry_date=date(2026, 1, 10),
        partner_id=customer.id,
        partner_on="debit",
    )
    _book(
        session,
        company,
        accounts["1400"],
        accounts["8400"],
        "50.00",
        entry_date=date(2026, 2, 10),
        partner_id=customer.id,
        partner_on="debit",
    )
    _book(
        session,
        company,
        accounts["1200"],
        accounts["1400"],
        "30.00",
        entry_date=date(2026, 2, 20),
        partner_id=customer.id,
        partner_on="credit",
    )
    # Partnerbezug außerhalb des Sammelkontos zählt nicht zum Partnerkonto.
    _book(
        session,
        company,
        accounts["4200"],
        accounts["1200"],
        "999.00",
        entry_date=date(2026, 2, 21),
        partner_id=customer.id,
        partner_on="debit",
    )

    statement = partner_statement(
        session=session,
        partner=customer,
        date_from=date(2026, 2, 1),
        date_to=date(2026, 2, 28),
    )
    assert [side["side"] for side in statement["sides"]] == ["debtor"]
    side = statement["sides"][0]
    assert side["number"] == customer.debtor_number
    assert side["opening_balance"] == Decimal("100.00")
    assert [line["balance"] for line in side["lines"]] == [Decimal("150.00"), Decimal("120.00")]
    assert side["debit_total"] == Decimal("50.00")
    assert side["credit_total"] == Decimal("30.00")
    assert side["closing_balance"] == Decimal("120.00")

    with pytest.raises(PartnerLedgerError):
        partner_statement(session=session, partner=customer, side="kaputt")
    with pytest.raises(PartnerLedgerError):
        partner_statement(
            session=session,
            partner=customer,
            date_from=date(2026, 3, 1),
            date_to=date(2026, 1, 1),
        )


def test_statement_paginates_but_keeps_running_balance(session: Session) -> None:
    company, accounts = _seed(session)
    customer = _customer(session, company)
    for day in range(1, 4):
        _book(
            session,
            company,
            accounts["1400"],
            accounts["8400"],
            "10.00",
            entry_date=date(2026, 1, day),
            partner_id=customer.id,
            partner_on="debit",
        )
    statement = partner_statement(session=session, partner=customer, limit=1, offset=2)
    side = statement["sides"][0]
    assert side["line_count"] == 3
    assert [line["balance"] for line in side["lines"]] == [Decimal("30.00")]


def test_balances_reconcile_with_ledger_and_open_items(session: Session) -> None:
    company, accounts = _seed(session)
    customer = _customer(session, company, "Kunde Eins")
    other = _customer(session, company, "Kunde Zwei")
    _book(
        session,
        company,
        accounts["1400"],
        accounts["8400"],
        "119.00",
        entry_date=date(2026, 1, 10),
        partner_id=customer.id,
        partner_on="debit",
    )
    # Zeile ohne Partner auf dem Sammelkonto.
    _book(
        session, company, accounts["1400"], accounts["8400"], "20.00", entry_date=date(2026, 1, 11)
    )
    # Später als der Stichtag: zählt nicht.
    _book(
        session,
        company,
        accounts["1400"],
        accounts["8400"],
        "1.00",
        entry_date=date(2026, 6, 1),
        partner_id=customer.id,
        partner_on="debit",
    )
    _item(
        session,
        company,
        accounts["1400"],
        reference="RE-1",
        partner_id=customer.id,
        entry_date=date(2026, 1, 10),
        amount=Decimal("119.00"),
    )
    # Posten ohne Buchung zeigt sich als Abweichung.
    _item(
        session,
        company,
        accounts["1400"],
        reference="RE-2",
        partner_id=other.id,
        entry_date=date(2026, 1, 12),
        amount=Decimal("40.00"),
    )

    report = partner_balances(
        session=session, company_id=company.id, side="debtor", as_of=date(2026, 3, 1)
    )
    rows = {row["name"]: row for row in report["partners"]}
    assert rows["Kunde Eins"]["balance"] == Decimal("119.00")
    assert rows["Kunde Eins"]["opos_difference"] == Decimal("0.00")
    assert rows["Kunde Zwei"]["balance"] == Decimal("0.00")
    assert rows["Kunde Zwei"]["opos_difference"] == Decimal("-40.00")

    account_row = next(row for row in report["accounts"] if row["code"] == "1400")
    assert account_row["partner_total"] == Decimal("119.00")
    assert account_row["unassigned"] == Decimal("20.00")
    assert account_row["ledger_balance"] == Decimal("139.00")
    assert report["totals"]["ledger_balance"] == (
        report["totals"]["partner_balance"] + report["totals"]["unassigned"]
    )


def test_creditor_balance_compares_with_positive_open_items(session: Session) -> None:
    company, accounts = _seed(session)
    supplier = _supplier(session, company)
    _book(
        session,
        company,
        accounts["4200"],
        accounts["1600"],
        "500.00",
        entry_date=date(2026, 1, 10),
        partner_id=supplier.id,
        partner_on="credit",
    )
    _item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        reference="ER-1",
        partner_id=supplier.id,
        entry_date=date(2026, 1, 10),
        amount=Decimal("500.00"),
    )
    report = partner_balances(
        session=session, company_id=company.id, side="creditor", as_of=date(2026, 3, 1)
    )
    row = report["partners"][0]
    assert row["number"] == supplier.creditor_number
    assert row["balance"] == Decimal("-500.00")
    assert row["opos_difference"] == Decimal("0.00")


def test_carryforward_does_not_double_balances(session: Session) -> None:
    company, accounts = _seed(session)
    customer = _customer(session, company)
    fiscal_year_entry = _book(
        session,
        company,
        accounts["1400"],
        accounts["8400"],
        "119.00",
        entry_date=date(2026, 3, 1),
        partner_id=customer.id,
        partner_on="debit",
    )
    fiscal_year = session.get(FiscalYear, fiscal_year_entry.fiscal_year_id)
    close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="t")

    report = partner_balances(
        session=session, company_id=company.id, side="debtor", as_of=date(2027, 6, 1)
    )
    assert report["partners"][0]["balance"] == Decimal("119.00")
    statement = partner_statement(session=session, partner=customer, date_from=date(2027, 1, 1))
    side = statement["sides"][0]
    assert side["opening_balance"] == Decimal("119.00")
    assert side["lines"] == []


# ---------------------------------------------------------------------------
# REST und Oberfläche
# ---------------------------------------------------------------------------


def _client(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'subledger.db'}"}
    )
    with app.extensions["db_session_factory"]() as db:
        company, accounts = _seed(db)
        db.add(
            User(
                username="admin",
                password_hash=hash_password("geheim123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db.commit()
        ids = {code: account.id for code, account in accounts.items()}
        company_id = company.id
        foreign, _ = _seed(db, "Fremd GmbH")
        foreign_partner = _customer(db, foreign, "Fremdkunde").id
    client = app.test_client()
    assert (
        client.post("/auth/login", data={"username": "admin", "password": "geheim123"}).status_code
        == 302
    )
    return client, company_id, ids, foreign_partner


def test_api_covers_partner_on_open_items_and_reports(tmp_path: Path) -> None:
    client, company_id, ids, foreign_partner = _client(tmp_path)
    customer = client.post(
        "/api/v1/partners",
        json={
            "company_id": company_id,
            "name": "API Kunde",
            "is_customer": True,
            "street": "Weg 2",
            "postal_code": "54321",
            "city": "Ort",
            "payment_term_days": 10,
        },
    ).get_json()
    created = client.post(
        "/api/v1/open-items",
        json={
            "company_id": company_id,
            "account_id": ids["1400"],
            "item_type": "receivable",
            "reference": "RE-API",
            "amount": "100.00",
            "entry_date": (date.today() - timedelta(days=30)).isoformat(),
            "partner_number": customer["debtor_number"],
        },
    )
    assert created.status_code == 201, created.get_json()
    item = created.get_json()
    assert item["partner_id"] == customer["id"]
    assert item["partner_number"] == customer["debtor_number"]
    assert item["partner_name"] == "API Kunde"
    assert item["counterparty"] == "API Kunde"
    assert item["due_date"] == (date.today() - timedelta(days=20)).isoformat()

    missing = client.post(
        "/api/v1/open-items",
        json={
            "company_id": company_id,
            "account_id": ids["1400"],
            "item_type": "receivable",
            "reference": "RE-X",
            "amount": "1.00",
            "partner_number": "99999",
        },
    )
    assert missing.status_code == 422

    foreign = client.post(
        f"/api/v1/open-items/{item['id']}/partner", json={"partner_id": foreign_partner}
    )
    assert foreign.status_code == 422

    listed = client.get(
        "/api/v1/open-items", query_string={"company_id": company_id, "q": "API Kun"}
    ).get_json()["open_items"]
    assert [row["reference"] for row in listed] == ["RE-API"]
    listed = client.get(
        "/api/v1/open-items",
        query_string={"company_id": company_id, "partner_id": customer["id"] + 999},
    ).get_json()["open_items"]
    assert listed == []

    dunning = client.get(
        "/api/v1/dunning-proposals", query_string={"company_id": company_id}
    ).get_json()["proposals"]
    assert dunning[0]["partner_number"] == customer["debtor_number"]

    booking = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-03-01",
            "description": "Rechnung",
            "lines": [
                {
                    "account_id": ids["1400"],
                    "debit_amount": "100.00",
                    "credit_amount": "0.00",
                    "partner_id": customer["id"],
                },
                {"account_id": ids["8400"], "debit_amount": "0.00", "credit_amount": "100.00"},
            ],
        },
    )
    assert booking.status_code == 201, booking.get_json()

    statement = client.get(f"/api/v1/partners/{customer['id']}/statement").get_json()
    assert statement["sides"][0]["closing_balance"] == "100.00"
    assert statement["sides"][0]["lines"][0]["entry_date"] == "2026-03-01"
    assert statement["open_items"][0]["reference"] == "RE-API"
    assert (
        client.get(
            f"/api/v1/partners/{customer['id']}/statement", query_string={"side": "x"}
        ).status_code
        == 400
    )

    balances = client.get(
        "/api/v1/reports/partner-balances",
        query_string={"company_id": company_id, "as_of": date.today().isoformat()},
    ).get_json()
    assert balances["side"] == "debtor"
    assert balances["partners"][0]["balance"] == "100.00"
    assert balances["partners"][0]["opos_difference"] == "0.00"
    assert balances["accounts"][0]["code"] == "1400"

    csv_response = client.get(
        "/api/v1/exports/partner-balances.csv", query_string={"company_id": company_id}
    )
    assert csv_response.status_code == 200
    assert "API Kunde" in csv_response.get_data(as_text=True)
    assert (
        client.get(
            "/api/v1/reports/partner-balances",
            query_string={"company_id": company_id, "as_of": "kaputt"},
        ).status_code
        == 400
    )


def test_payment_run_api_reports_recipient_and_warnings(tmp_path: Path) -> None:
    client, company_id, ids, _ = _client(tmp_path)
    supplier = client.post(
        "/api/v1/partners",
        json={"company_id": company_id, "name": "API Lieferant", "is_supplier": True},
    ).get_json()
    assert (
        client.post(
            f"/api/v1/partners/{supplier['id']}/bank-details", json={"iban": PARTNER_IBAN}
        ).status_code
        == 200
    )
    created = client.post(
        "/api/v1/open-items",
        json={
            "company_id": company_id,
            "account_id": ids["1600"],
            "item_type": "payable",
            "reference": "ER-API",
            "amount": "50.00",
            "partner_id": supplier["id"],
            "counterparty_iban": OTHER_IBAN,
        },
    ).get_json()
    proposal = client.get(
        "/api/v1/payment-runs/proposals", query_string={"company_id": company_id}
    ).get_json()["items"][0]
    assert proposal["iban_mismatch"] is True
    assert proposal["partner_iban"] == PARTNER_IBAN
    assert proposal["counterparty"] == "API Lieferant"

    result = client.post(
        "/api/v1/payment-runs",
        json={"company_id": company_id, "open_item_ids": [created["id"]]},
    ).get_json()
    assert result["warnings"]
    xml_bytes = base64.b64decode(result["xml_base64"])
    assert _pain_recipients(xml_bytes) == [("API Lieferant", OTHER_IBAN)]


def test_ui_pages_show_partner(tmp_path: Path) -> None:
    client, company_id, ids, _ = _client(tmp_path)
    customer = client.post(
        "/api/v1/partners",
        json={
            "company_id": company_id,
            "name": "UI Kunde",
            "is_customer": True,
            "street": "Allee 3",
            "postal_code": "11111",
            "city": "Stadt",
        },
    ).get_json()
    item = client.post(
        "/api/v1/open-items",
        json={
            "company_id": company_id,
            "account_id": ids["1400"],
            "item_type": "receivable",
            "reference": "RE-UI",
            "amount": "75.00",
            "due_date": (date.today() - timedelta(days=5)).isoformat(),
            "counterparty": "Freitext",
        },
    ).get_json()

    with client.session_transaction() as flask_session:
        csrf = flask_session.get("_csrf_token")
    assigned = client.post(
        f"/offene-posten/{item['id']}/partner",
        data={"partner_id": customer["id"], "_csrf_token": csrf or ""},
    )
    assert assigned.status_code == 302

    page = client.get("/offene-posten", query_string={"company_id": company_id}).get_data(
        as_text=True
    )
    assert "UI Kunde" in page
    filtered = client.get(
        "/offene-posten", query_string={"company_id": company_id, "partner_id": customer["id"]}
    ).get_data(as_text=True)
    assert "Filter aufheben" in filtered

    dunning = client.get("/mahnwesen", query_string={"company_id": company_id}).get_data(
        as_text=True
    )
    assert "UI Kunde" in dunning
    letter = client.get(f"/mahnwesen/{item['id']}/schreiben").get_data(as_text=True)
    assert "Allee 3" in letter
    assert "11111 Stadt" in letter
    assert "bitte vor dem Druck" not in letter

    detail = client.get(f"/partner/{customer['id']}").get_data(as_text=True)
    assert "Partnerkonto" in detail
    assert "RE-UI" in detail

    balances = client.get(
        "/partner/saldenliste", query_string={"company_id": company_id, "side": "creditor"}
    ).get_data(as_text=True)
    assert "Saldenliste Kreditoren" in balances
    assert "1600" in balances
    balances = client.get(
        "/partner/saldenliste", query_string={"company_id": company_id}
    ).get_data(as_text=True)
    assert "UI Kunde" in balances


def test_partner_assignment_and_reports_respect_rights_and_tenants(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "API_REQUIRE_AUTH": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'rights.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db:
        company, accounts = _seed(db)
        foreign, foreign_accounts = _seed(db, "Fremd GmbH")
        foreign_customer = _customer(db, foreign, "Fremdkunde")
        own_item = _item(db, company, accounts["1400"])
        foreign_item = _item(db, foreign, foreign_accounts["1400"], reference="RE-F")
        for username, role, token in (
            ("pruefer", "Pruefer", "obk_pruefer-sub"),
            ("buchhalter", "Buchhalter", "obk_buchhalter-sub"),
        ):
            db.add(
                User(
                    username=username,
                    password_hash=hash_password("geheim123"),
                    role=role,
                    tenant_id=company.tenant_id,
                    api_token_hash=hash_api_token(token),
                    api_token_last4=token[-4:],
                )
            )
        db.commit()
        own_item_id, foreign_item_id = own_item.id, foreign_item.id
        foreign_company_id, foreign_partner_id = foreign.id, foreign_customer.id
        company_id = company.id

    client = app.test_client()
    reader = {"Authorization": "Bearer obk_pruefer-sub"}
    writer = {"Authorization": "Bearer obk_buchhalter-sub"}
    assert (
        client.post(
            f"/api/v1/open-items/{own_item_id}/partner", headers=reader, json={}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/open-items/{foreign_item_id}/partner", headers=writer, json={}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/open-items/{own_item_id}/partner",
            headers=writer,
            json={"partner_id": foreign_partner_id},
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/partners/{foreign_partner_id}/statement", headers=writer
        ).status_code
        == 404
    )
    for path in ("/api/v1/reports/partner-balances", "/api/v1/exports/partner-balances.csv"):
        assert (
            client.get(
                path, headers=writer, query_string={"company_id": foreign_company_id}
            ).status_code
            == 404
        )
        assert (
            client.get(path, headers=reader, query_string={"company_id": company_id}).status_code
            == 200
        )
