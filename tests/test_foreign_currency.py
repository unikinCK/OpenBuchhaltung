"""Fremdwährung: Kurse, Buchungszeilen, offene Posten, Kursdifferenz, Bank, Rechnung, Bewertung."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, create_session_factory
from app.services.audit_export import EXPORT_TABLE_MODELS
from app.services.bank_import import (
    BankImportError,
    bank_reconciliation,
    book_transaction,
    detect_transfer_counterparts,
    match_transaction,
    reassign_bank_transactions,
    suggest_matches,
)
from app.services.compliance_integrity import (
    JOURNAL_CONTENT_HASH_VERSION,
    verify_compliance_integrity,
)
from app.services.dashboard import bank_account_rows
from app.services.dunning import dunning_proposals, serialize_proposal
from app.services.einvoice_import import book_einvoice, parse_einvoice
from app.services.exchange_rates import (
    ExchangeRateError,
    fetch_ecb_rates,
    find_exchange_rate,
    format_rate_text,
    fx_valuation_report,
    parse_ecb_rates,
    post_fx_valuation,
    rate_for,
    set_exchange_rate,
    to_company_amount,
)
from app.services.incoming_invoice import IncomingInvoiceError
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entry,
    first_open_posting_date,
    reverse_journal_entry,
)
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    settle_open_item,
)
from app.services.partners import create_partner
from app.services.periods import lock_period
from app.services.sales_invoice_pdf import pdf_text
from app.services.sales_invoices import (
    SalesInvoiceError,
    SalesInvoiceStateError,
    cancel_sales_invoice,
    create_sales_invoice,
    issue_sales_invoice,
    serialize_sales_invoice,
    update_sales_invoice,
)
from app.services.sepa_export import SepaExportError, create_payment_run, payable_items_for_run
from app.services.tax_codes import ensure_default_tax_codes
from app.services.vat_returns import compute_vat_return_details, set_company_vat_method
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    Document,
    ExchangeRate,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    OpenItemSettlement,
    TaxCode,
    Tenant,
    User,
)

ECB_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
    xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube>
    <Cube time="2026-03-03">
      <Cube currency="USD" rate="1.0800"/>
      <Cube currency="CHF" rate="0.9500"/>
    </Cube>
    <Cube time="2026-03-02">
      <Cube currency="USD" rate="1.0700"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session, currency: str = "EUR") -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name="FX Mandant")
    company = Company(
        tenant=tenant,
        name="FX GmbH",
        currency_code=currency,
        street="Hauptstr. 1",
        postal_code="12345",
        city="Musterstadt",
        country_code="DE",
        vat_id="DE123456789",
        tax_number="12/345/67890",
        phone="+49 30 1",
        email="info@example.org",
        iban="DE89370400440532013000",
    )
    session.add_all([tenant, company])
    session.flush()
    accounts: dict[str, Account] = {}
    for code, name, account_type, subledger in (
        ("1200", "Bank", "asset", None),
        ("1210", "Bank USD", "asset", None),
        ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
        ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", "creditor"),
        ("1576", "Abziehbare Vorsteuer 19 %", "asset", None),
        ("1776", "Umsatzsteuer 19 %", "liability", None),
        ("1771", "Umsatzsteuer 7 %", "liability", None),
        ("1571", "Abziehbare Vorsteuer 7 %", "asset", None),
        ("2150", "Aufwendungen aus Kursdifferenzen", "expense", None),
        ("2660", "Erträge aus Kursdifferenzen", "income", None),
        ("8400", "Erlöse 19 % USt", "income", None),
        ("8338", "Erlöse Drittland", "income", None),
        ("4210", "Miete", "expense", None),
    ):
        account = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
            subledger=subledger,
        )
        session.add(account)
        accounts[code] = account
    session.commit()
    ensure_default_tax_codes(session=session, company=company)
    session.commit()
    return company, accounts


def _rate(session, company, day=date(2026, 3, 3), rate="1.0800", currency="USD"):
    return set_exchange_rate(
        session=session,
        company=company,
        currency_code=currency,
        rate_date=day,
        rate=rate,
        changed_by="t",
    )


def _fx_item(session, company, account, *, item_type="receivable", foreign="1080.00", **fields):
    values = {
        "company_id": company.id,
        "account_id": account.id,
        "item_type": item_type,
        "reference": "FX-1",
        "entry_date": date(2026, 3, 3),
        "amount": Decimal("0"),
        "changed_by": "t",
        "foreign_currency_code": "USD",
        "foreign_amount": Decimal(foreign),
        "exchange_rate": Decimal("1.08"),
    }
    values.update(fields)
    return create_open_item(session=session, payload=OpenItemInput(**values))


def _book(session, company, debit, credit, amount, *, day=date(2026, 3, 20)):
    value = Decimal(amount)
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description="Zahlung",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(account_id=debit.id, debit_amount=value),
                JournalLineInput(account_id=credit.id, credit_amount=value),
            ],
        ),
    )


def _balance(session, account: Account) -> Decimal:
    rows = session.execute(
        select(JournalEntryLine.debit_amount, JournalEntryLine.credit_amount).where(
            JournalEntryLine.account_id == account.id
        )
    ).all()
    return sum((debit - credit for debit, credit in rows), Decimal("0.00"))


# ---------------------------------------------------------------------------
# Kurse
# ---------------------------------------------------------------------------


def test_rate_lookup_uses_latest_within_ten_days(session: Session) -> None:
    company, _ = _seed(session)
    _rate(session, company, day=date(2026, 3, 2), rate="1.07")
    _rate(session, company, day=date(2026, 3, 6), rate="1.09")
    tenant_id = company.tenant_id
    assert rate_for(
        session=session, tenant_id=tenant_id, currency_code="USD", on=date(2026, 3, 5)
    ) == Decimal("1.07")
    assert rate_for(
        session=session, tenant_id=tenant_id, currency_code="USD", on=date(2026, 3, 6)
    ) == Decimal("1.09")
    assert (
        find_exchange_rate(
            session=session, tenant_id=tenant_id, currency_code="USD", on=date(2026, 3, 1)
        )
        is None
    )
    with pytest.raises(ExchangeRateError, match="keinen Kurs"):
        rate_for(session=session, tenant_id=tenant_id, currency_code="USD", on=date(2026, 4, 1))
    with pytest.raises(ExchangeRateError, match="keinen Kurs"):
        rate_for(session=session, tenant_id=tenant_id, currency_code="GBP", on=date(2026, 3, 6))


def test_set_rate_validates_and_updates_with_audit(session: Session) -> None:
    company, _ = _seed(session)
    for currency, rate, message in (
        ("US", "1.08", "drei Buchstaben"),
        ("EUR", "1.08", "Firmenwährung"),
        ("USD", "0", "größer 0"),
        ("USD", "abc", "Ungültiger Kurs"),
        ("USD", "NaN", "größer 0"),
    ):
        with pytest.raises(ExchangeRateError, match=message):
            _rate(session, company, currency=currency, rate=rate)
    first = _rate(session, company, rate="1,08")
    assert first.rate == Decimal("1.080000")
    second = _rate(session, company, rate="1.09")
    assert second.id == first.id
    assert second.rate == Decimal("1.090000")
    payloads = session.execute(
        select(AuditLog.payload).where(AuditLog.entity_type == "exchange_rate")
    ).scalars().all()
    assert len(payloads) == 2


def test_foreign_currency_requires_euro_as_company_currency(session: Session) -> None:
    company, accounts = _seed(session, currency="CHF")
    with pytest.raises(ExchangeRateError, match="EUR als Firmenwährung"):
        _rate(session, company)
    with pytest.raises(OpenItemError, match="EUR als Firmenwährung"):
        _fx_item(session, company, accounts["1400"])


def test_parse_and_fetch_ecb_rates_keep_manual_rates(session: Session) -> None:
    assert parse_ecb_rates(ECB_XML)[0] == (date(2026, 3, 3), "USD", Decimal("1.080000"))
    for broken in (b"kein xml", b"<a/>"):
        with pytest.raises(ExchangeRateError):
            parse_ecb_rates(broken)

    company, _ = _seed(session)
    manual = _rate(session, company, day=date(2026, 3, 3), rate="1.5")
    result = fetch_ecb_rates(
        session=session, company=company, changed_by="t", download=lambda: ECB_XML
    )
    assert result["created"] == 2
    assert result["skipped"] == 1
    assert result["latest_rate_date"] == date(2026, 3, 3)
    session.refresh(manual)
    assert manual.rate == Decimal("1.500000")
    assert manual.source == "manual"
    sources = {
        (row.currency_code, row.rate_date): row.source
        for row in session.execute(select(ExchangeRate)).scalars()
    }
    assert sources[("CHF", date(2026, 3, 3))] == "ecb"
    assert sources[("USD", date(2026, 3, 2))] == "ecb"
    # Erneuter Abruf legt nichts doppelt an.
    assert (
        fetch_ecb_rates(
            session=session, company=company, changed_by="t", download=lambda: ECB_XML
        )["created"]
        == 0
    )


def test_rate_text_and_conversion() -> None:
    company = Company(name="x", currency_code="EUR")
    assert format_rate_text(company, "USD", Decimal("1.080000")) == "1 EUR = 1,08 USD"
    assert to_company_amount(Decimal("100.00"), Decimal("1.08")) == Decimal("92.59")


# ---------------------------------------------------------------------------
# Buchungszeile und Hash
# ---------------------------------------------------------------------------


def _fx_entry(session, company, accounts, **line_fields):
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 3, 3),
            description="Fremdwährung",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(
                    account_id=accounts["1210"].id,
                    debit_amount=Decimal("100.00"),
                    **line_fields,
                ),
                JournalLineInput(account_id=accounts["8338"].id, credit_amount=Decimal("100.00")),
            ],
        ),
    )


def test_line_stores_foreign_amount_and_derives_rate(session: Session) -> None:
    company, accounts = _seed(session)
    entry = _fx_entry(
        session,
        company,
        accounts,
        foreign_currency_code="usd",
        foreign_amount=Decimal("108.00"),
    )
    line = next(line for line in entry.lines if line.account_id == accounts["1210"].id)
    assert line.foreign_currency_code == "USD"
    assert line.foreign_amount == Decimal("108.00")
    assert line.exchange_rate == Decimal("1.080000")
    assert line.currency_code == "EUR"
    assert line.debit_amount == Decimal("100.00")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"foreign_currency_code": "USD"}, "größer 0"),
        ({"foreign_amount": Decimal("108.00")}, "drei Buchstaben"),
        ({"exchange_rate": Decimal("1.08")}, "braucht Fremdwährung"),
        (
            {"foreign_currency_code": "EUR", "foreign_amount": Decimal("108.00")},
            "Firmenwährung",
        ),
        (
            {
                "foreign_currency_code": "USD",
                "foreign_amount": Decimal("108.00"),
                "exchange_rate": Decimal("2"),
            },
            "ergeben zum Kurs",
        ),
        (
            {
                "foreign_currency_code": "USD",
                "foreign_amount": Decimal("108.00"),
                "exchange_rate": Decimal("0"),
            },
            "größer 0",
        ),
    ],
)
def test_line_foreign_validation(session: Session, fields, message) -> None:
    company, accounts = _seed(session)
    with pytest.raises(JournalEntryCreationError, match=message):
        _fx_entry(session, company, accounts, **fields)


def test_seal_covers_foreign_fields_and_storno_mirrors_them(session: Session) -> None:
    company, accounts = _seed(session)
    entry = _fx_entry(
        session,
        company,
        accounts,
        foreign_currency_code="USD",
        foreign_amount=Decimal("108.00"),
        exchange_rate=Decimal("1.08"),
    )
    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
    session.refresh(entry)
    assert entry.content_hash_version == JOURNAL_CONTENT_HASH_VERSION == 5
    assert verify_compliance_integrity(session=session, company_id=company.id).valid is True

    # Ein geänderter Kurs bricht das Siegel; im Test ohne Trigger direkt änderbar.
    line = next(line for line in entry.lines if line.foreign_currency_code)
    line.exchange_rate = Decimal("1.50")
    session.flush()
    assert verify_compliance_integrity(session=session, company_id=company.id).valid is False
    session.rollback()

    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 3, 4),
        changed_by="t",
    )
    mirrored = next(line for line in reversal.lines if line.foreign_currency_code)
    assert mirrored.foreign_amount == Decimal("108.00")
    assert mirrored.credit_amount == Decimal("100.00")


# ---------------------------------------------------------------------------
# Offene Posten und Kursdifferenz
# ---------------------------------------------------------------------------


def test_open_item_converts_with_given_or_daily_rate(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    assert item.original_amount == Decimal("1000.00")
    assert item.open_amount == Decimal("1000.00")
    assert item.foreign_open_amount == Decimal("1080.00")
    assert item.currency_code == "EUR"

    with pytest.raises(OpenItemError, match="keinen Kurs"):
        _fx_item(session, company, accounts["1400"], reference="FX-2", exchange_rate=None)
    _rate(session, company)
    daily = _fx_item(session, company, accounts["1400"], reference="FX-3", exchange_rate=None)
    assert daily.exchange_rate == Decimal("1.080000")

    with pytest.raises(OpenItemError, match="Firmenwährung"):
        _fx_item(
            session, company, accounts["1400"], reference="FX-4", foreign_currency_code="EUR"
        )
    with pytest.raises(OpenItemError, match="größer 0"):
        _fx_item(session, company, accounts["1400"], reference="FX-5", foreign="0")


def test_receivable_gain_is_booked_and_clears_the_collective_account(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    # 1.080 USD bringen zum schwächeren Euro 1.028,57 EUR statt 1.000 EUR.
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1028.57")
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1028.57"),
    )
    assert settled.status == "settled"
    assert settled.open_amount == Decimal("0.00")
    assert settled.foreign_open_amount == Decimal("0.00")

    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.amount == Decimal("1000.00")
    assert row.paid_amount == Decimal("1028.57")
    assert row.foreign_amount == Decimal("1080.00")
    assert row.fx_difference == Decimal("28.57")
    assert _balance(session, accounts["1400"]) == Decimal("0.00")
    assert _balance(session, accounts["2660"]) == Decimal("-28.57")
    assert _balance(session, accounts["2150"]) == Decimal("0.00")


def test_receivable_loss_is_booked(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "950.00")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("950.00"),
    )
    assert _balance(session, accounts["1400"]) == Decimal("0.00")
    assert _balance(session, accounts["2150"]) == Decimal("50.00")
    assert _balance(session, accounts["2660"]) == Decimal("0.00")


@pytest.mark.parametrize(
    ("paid", "loss", "gain"),
    [("1050.00", "50.00", "0.00"), ("950.00", "0.00", "-50.00")],
)
def test_payable_differences_have_the_opposite_direction(
    session: Session, paid: str, loss: str, gain: str
) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["4210"], accounts["1600"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1600"], item_type="payable")
    payment = _book(session, company, accounts["1600"], accounts["1200"], paid)
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal(paid),
    )
    assert _balance(session, accounts["1600"]) == Decimal("0.00")
    assert _balance(session, accounts["2150"]) == Decimal(loss)
    assert _balance(session, accounts["2660"]) == Decimal(gain)


def test_partial_foreign_payments_leave_no_rounding_rest(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(
        session, company, accounts["1400"], foreign="100.00", exchange_rate=Decimal("1.07")
    )
    assert item.original_amount == Decimal("93.46")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        foreign_amount=Decimal("33.33"),
        exchange_rate=Decimal("1.07"),
    )
    session.refresh(item)
    assert item.status == "open"
    assert item.foreign_open_amount == Decimal("66.67")
    assert item.open_amount == Decimal("62.31")
    with pytest.raises(OpenItemError, match="übersteigt"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            foreign_amount=Decimal("66.68"),
            exchange_rate=Decimal("1.07"),
        )
    settled = settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", exchange_rate=Decimal("1.07")
    )
    assert settled.status == "settled"
    assert settled.open_amount == Decimal("0.00")
    rows = session.execute(select(OpenItemSettlement)).scalars().all()
    assert sum(row.amount for row in rows) == Decimal("93.46")


def test_settlement_without_booking_records_but_does_not_book_the_difference(
    session: Session,
) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        foreign_amount=Decimal("1080"),
        amount=Decimal("990"),
    )
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.fx_difference == Decimal("-10.00")
    assert row.fx_journal_entry_id is None
    assert _balance(session, accounts["2150"]) == Decimal("0.00")


def test_missing_fx_account_blocks_booking(session: Session) -> None:
    company, accounts = _seed(session)
    accounts["2660"].is_active = False
    session.commit()
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    with pytest.raises(OpenItemError, match="Erträge aus Kursdifferenzen"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            journal_entry_id=payment.id,
            foreign_amount=Decimal("1080.00"),
            amount=Decimal("1010.00"),
        )


def test_reversing_the_payment_restores_item_and_difference(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1028.57")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1028.57"),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    session.refresh(item)
    assert item.status == "open"
    assert item.open_amount == Decimal("1000.00")
    assert item.foreign_open_amount == Decimal("1080.00")
    assert _balance(session, accounts["2660"]) == Decimal("0.00")
    assert _balance(session, accounts["1400"]) == Decimal("1000.00")


def test_euro_item_rejects_foreign_bank_transaction(session: Session) -> None:
    company, accounts = _seed(session)
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=accounts["1400"].id,
            item_type="receivable",
            reference="EUR-1",
            entry_date=date(2026, 3, 3),
            amount=Decimal("100.00"),
            changed_by="t",
        ),
    )
    transaction = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    with pytest.raises(OpenItemError, match="läuft in USD"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
        )


def test_euro_bank_payment_of_foreign_item_needs_both_amounts(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    transaction = _booked_bank_tx(session, company, accounts, "1010.00", "EUR")
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=transaction.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1010.00"),
    )
    assert settled.status == "settled"
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.paid_amount == Decimal("1010.00")
    assert row.fx_difference == Decimal("10.00")
    assert row.fx_journal_entry_id is not None
    assert _balance(session, accounts["1400"]) == Decimal("0.00")


def test_unbooked_bank_transaction_is_refused(session: Session) -> None:
    """Sonst stünde die Zahlung nie im Hauptbuch und die Differenz bliebe ungebucht."""
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    usd = _bank_tx(session, company, accounts["1210"], "1080.00", "USD")
    euro = _bank_tx(session, company, accounts["1200"], "1000.00", "EUR")
    for transaction in (usd, euro):
        with pytest.raises(OpenItemError, match="noch nicht gebucht"):
            settle_open_item(
                session=session,
                open_item_id=item.id,
                changed_by="t",
                bank_transaction_id=transaction.id,
                foreign_amount=Decimal("1080.00"),
                amount=Decimal("1000.00"),
            )


def test_euro_part_payment_does_not_close_the_item(session: Session) -> None:
    """850 EUR auf einen Buchwert von 1.000 EUR sind eine Teilzahlung, kein Kursverlust."""
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    transaction = _booked_bank_tx(session, company, accounts, "850.00", "EUR")
    with pytest.raises(OpenItemError, match="wie viel USD"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
        )
    with pytest.raises(OpenItemError, match="wie viel USD"):
        settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", amount=Decimal("850.00")
        )
    with pytest.raises(OpenItemError, match="gezahlten Betrag in EUR"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
            foreign_amount=Decimal("918.00"),
        )
    session.refresh(item)
    assert item.status == "open"
    assert item.foreign_open_amount == Decimal("1080.00")

    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=transaction.id,
        foreign_amount=Decimal("918.00"),
        amount=Decimal("850.00"),
    )
    assert settled.status == "open"
    assert settled.foreign_open_amount == Decimal("162.00")
    assert settled.open_amount == Decimal("150.00")
    assert session.execute(select(OpenItemSettlement)).scalar_one().fx_difference == Decimal("0.00")


def test_full_settlement_without_a_named_payment_uses_the_rate(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    settled = settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", exchange_rate=Decimal("1.20")
    )
    assert settled.status == "settled"
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.paid_amount == Decimal("900.00")
    assert row.fx_difference == Decimal("-100.00")


def _booked_usd_payment(session, company, accounts, amount, *, rate="1.00"):
    return _booked_bank_tx(session, company, accounts, amount, "USD", rate=rate)


def test_overpayment_in_foreign_currency_invents_no_gain(session: Session) -> None:
    """2.000 USD auf einen Posten über 1.080 USD: Nur der Anteil zählt als Zahlung des Postens."""
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    transaction = _booked_usd_payment(session, company, accounts, "2000.00")
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=transaction.id,
    )
    assert settled.status == "settled"
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.foreign_amount == Decimal("1080.00")
    assert row.paid_amount == Decimal("1080.00")
    assert row.fx_difference == Decimal("80.00")
    assert _balance(session, accounts["2660"]) == Decimal("-80.00")
    # Die Überzahlung von 920 USD bleibt als Guthaben des Kunden auf dem Sammelkonto.
    assert _balance(session, accounts["1400"]) == Decimal("-920.00")


def test_foreign_collective_payment_is_shared_without_rest(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    first = _fx_item(session, company, accounts["1400"], foreign="648.00")
    second = _fx_item(session, company, accounts["1400"], foreign="432.00", reference="FX-2")
    third = _fx_item(session, company, accounts["1400"], foreign="108.00", reference="FX-3")
    transaction = _booked_usd_payment(session, company, accounts, "1080.00", rate="1.05")
    for item in (first, second):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
        )
    rows = session.execute(select(OpenItemSettlement)).scalars().all()
    assert sum(row.foreign_amount for row in rows) == Decimal("1080.00")
    assert sum(row.paid_amount for row in rows) == Decimal("1028.57")
    with pytest.raises(OpenItemError, match="schon zugeordnet"):
        settle_open_item(
            session=session,
            open_item_id=third.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
        )
    # 1.000 EUR Forderung für die ersten beiden, gezahlt 1.028,57 EUR, Differenz als Ertrag.
    assert _balance(session, accounts["1400"]) == Decimal("0.00")
    assert _balance(session, accounts["2660"]) == Decimal("-28.57")


def test_foreign_amount_above_the_payment_is_refused(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    transaction = _booked_usd_payment(session, company, accounts, "500.00", rate="1.08")
    with pytest.raises(OpenItemError, match="übersteigt die Zahlung"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
            foreign_amount=Decimal("600.00"),
            exchange_rate=Decimal("1.08"),
        )


def test_payment_line_without_partner_counts_for_item_with_partner(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    item = _fx_item(session, company, accounts["1400"], partner_id=customer.id)
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1020.00")
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1020.00"),
    )
    assert settled.status == "settled"
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.fx_journal_entry_id is not None


def test_collective_euro_payment_needs_the_amounts_of_the_item(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "2000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    other = _fx_item(session, company, accounts["1400"], reference="FX-2")
    payment = _book(session, company, accounts["1200"], accounts["1400"], "2030.00")
    with pytest.raises(OpenItemError, match="wie viel USD"):
        settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1010.00"),
    )
    assert settled.status == "settled"
    # Mehr als die Buchung hergibt, lässt sich nicht zuordnen.
    with pytest.raises(OpenItemError, match="schon zugeordnet"):
        settle_open_item(
            session=session,
            open_item_id=other.id,
            changed_by="t",
            journal_entry_id=payment.id,
            foreign_amount=Decimal("1080.00"),
            amount=Decimal("1020.01"),
        )
    settle_open_item(
        session=session,
        open_item_id=other.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1020.00"),
    )
    assert _balance(session, accounts["1400"]) == Decimal("0.00")
    assert _balance(session, accounts["2660"]) == Decimal("-30.00")


def test_payment_without_collective_line_books_no_difference(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["8338"], "1050.00")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1050.00"),
    )
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.fx_difference == Decimal("50.00")
    assert row.fx_journal_entry_id is None
    assert _balance(session, accounts["1400"]) == Decimal("1000.00")


def test_reversing_a_payment_linked_only_by_bank_transaction_restores_all(
    session: Session,
) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    transaction = _booked_usd_payment(session, company, accounts, "1080.00", rate="1.05")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=transaction.id,
    )
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.journal_entry_id == transaction.journal_entry_id
    assert _balance(session, accounts["2660"]) == Decimal("-28.57")
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    session.refresh(item)
    assert item.status == "open"
    assert item.foreign_open_amount == Decimal("1080.00")
    assert _balance(session, accounts["2660"]) == Decimal("0.00")
    assert _balance(session, accounts["1400"]) == Decimal("1000.00")


def test_fx_difference_cannot_be_reversed_alone(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1010.00"),
    )
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    with pytest.raises(JournalEntryCreationError, match="mit dem Storno dieser Zahlung"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=row.fx_journal_entry_id,
            reversal_date=date(2026, 3, 25),
            changed_by="t",
        )


def _settle_1010(session, item, payment):
    return settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1010.00"),
    )


def _difference_entry(session) -> JournalEntry:
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    return session.get(JournalEntry, row.fx_journal_entry_id)


def test_difference_leaves_a_closed_year_for_the_next_open_date(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    session.execute(select(FiscalYear).where(FiscalYear.company_id == company.id)).scalar_one(
    ).is_closed = True
    session.commit()
    _settle_1010(session, item, payment)
    assert _difference_entry(session).entry_date == date(2027, 1, 1)
    # Das Zahlungsdatum bleibt das der Buchung, es bestimmt die Istversteuerung.
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    assert row.settlement_date == date(2026, 3, 20)


def test_difference_leaves_locked_months_for_the_next_open_date(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    april = _book(
        session, company, accounts["1200"], accounts["8338"], "1.00", day=date(2026, 4, 2)
    )
    lock_period(session=session, period_id=payment.period_id, locked_by="t")
    lock_period(session=session, period_id=april.period_id, locked_by="t")
    assert first_open_posting_date(
        session=session, company_id=company.id, on=date(2026, 2, 10)
    ) == date(2026, 2, 10)
    _settle_1010(session, item, payment)
    assert _difference_entry(session).entry_date == date(2026, 5, 1)


def test_payment_reversed_before_the_difference_date_reverses_both(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1010.00"),
        settlement_date=date(2026, 4, 5),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    session.refresh(item)
    assert item.status == "open"
    assert _balance(session, accounts["2660"]) == Decimal("0.00")


def test_booking_error_of_the_difference_is_a_clean_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")

    def refuse(**_kwargs):
        raise JournalEntryCreationError("Nicht möglich.")

    monkeypatch.setattr("app.services.open_items.create_journal_entry", refuse)
    with pytest.raises(OpenItemError, match="Kursdifferenz nicht gebucht"):
        _settle_1010(session, item, payment)


def test_reversed_entry_and_reversal_cannot_settle(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    with pytest.raises(OpenItemError, match="ist storniert"):
        _settle_1010(session, item, payment)
    with pytest.raises(OpenItemError, match="Stornobuchung"):
        _settle_1010(session, item, reversal)


def _lined_payment(session, company, lines, day=date(2026, 3, 20)):
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description="Zahlung",
            status="posted",
            changed_by="t",
            lines=lines,
        ),
    )


def _euro_item(session, company, account, amount, reference="EUR-1"):
    return create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=account.id,
            item_type="receivable",
            reference=reference,
            entry_date=date(2026, 3, 3),
            amount=Decimal(amount),
            changed_by="t",
        ),
    )


def test_reversed_entry_cannot_settle_or_match_through_a_bank_transaction(
    session: Session,
) -> None:
    company, accounts = _seed(session)
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1020.00")
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    transaction = _bank_tx(session, company, accounts["1200"], "1020.00", "EUR")
    assert suggest_matches(session=session, transaction=transaction) == []
    with pytest.raises(BankImportError, match="ist storniert"):
        match_transaction(
            session=session,
            transaction_id=transaction.id,
            journal_entry_id=payment.id,
            changed_by="t",
        )
    # Auch ein schon vorhandener Link führt nicht zum Ausgleich.
    transaction.journal_entry_id = payment.id
    transaction.status = "matched"
    session.commit()
    with pytest.raises(OpenItemError, match="ist storniert"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            bank_transaction_id=transaction.id,
            foreign_amount=Decimal("1080.00"),
            amount=Decimal("1020.00"),
        )
    assert reversal.reversal_of_id == payment.id


def test_mixed_payment_lines_serve_their_own_currency(session: Session) -> None:
    """Die Reihenfolge der Ausgleiche ändert nichts, keine Zeile belegt eine andere."""
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "450.00", day=date(2026, 3, 3))
    euro = _euro_item(session, company, accounts["1400"], "50.00")
    usd = _fx_item(session, company, accounts["1400"], foreign="108.00")
    chf = _fx_item(
        session,
        company,
        accounts["1400"],
        reference="FX-CHF",
        foreign="300.00",
        foreign_currency_code="CHF",
        exchange_rate=Decimal("1.00"),
    )
    payment = _lined_payment(
        session,
        company,
        [
            JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal("450.00")),
            JournalLineInput(account_id=accounts["1400"].id, credit_amount=Decimal("50.00")),
            JournalLineInput(
                account_id=accounts["1400"].id,
                credit_amount=Decimal("100.00"),
                foreign_currency_code="USD",
                foreign_amount=Decimal("108.00"),
            ),
            JournalLineInput(
                account_id=accounts["1400"].id,
                credit_amount=Decimal("300.00"),
                foreign_currency_code="CHF",
                foreign_amount=Decimal("300.00"),
            ),
        ],
    )
    for item in (euro, usd, chf):
        settled = settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )
        assert settled.status == "settled"
    rows = session.execute(select(OpenItemSettlement)).scalars().all()
    assert [row.fx_difference for row in rows] == [None, Decimal("0.00"), Decimal("0.00")]
    assert _balance(session, accounts["1400"]) == Decimal("0.00")
    assert _balance(session, accounts["2150"]) == Decimal("0.00")


@pytest.mark.parametrize(("first", "second"), [("200.00", "100.00"), ("100.00", "300.00")])
def test_lines_of_one_partner_are_not_taken_by_another(
    session: Session, first: str, second: str
) -> None:
    company, accounts = _seed(session)
    partners = [
        create_partner(
            session=session, company=company, changed_by="t", name=name, is_customer=True
        )
        for name in ("Kunde A", "Kunde B")
    ]
    session.commit()
    items = [
        _fx_item(
            session,
            company,
            accounts["1400"],
            reference=partner.name,
            partner_id=partner.id,
            foreign=amount,
            exchange_rate=Decimal("1.00"),
        )
        for partner, amount in zip(partners, (first, second))
    ]
    total = Decimal(first) + Decimal(second)
    payment = _lined_payment(
        session,
        company,
        [
            JournalLineInput(account_id=accounts["1200"].id, debit_amount=total),
            *(
                JournalLineInput(
                    account_id=accounts["1400"].id,
                    credit_amount=Decimal(amount),
                    partner_id=partner.id,
                    foreign_currency_code="USD",
                    foreign_amount=Decimal(amount),
                )
                for partner, amount in zip(partners, (first, second))
            ),
        ],
    )
    for item in items:
        settled = settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )
        assert settled.status == "settled"
        assert settled.foreign_open_amount == Decimal("0.00")


def test_line_without_partner_is_not_used_twice(session: Session) -> None:
    company, accounts = _seed(session)
    partners = [
        create_partner(
            session=session, company=company, changed_by="t", name=name, is_customer=True
        )
        for name in ("Kunde A", "Kunde B")
    ]
    session.commit()
    items = [
        _fx_item(
            session,
            company,
            accounts["1400"],
            reference=partner.name,
            partner_id=partner.id,
            foreign="100.00",
            exchange_rate=Decimal("1.00"),
        )
        for partner in partners
    ]
    payment = _lined_payment(
        session,
        company,
        [
            JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal("100.00")),
            JournalLineInput(
                account_id=accounts["1400"].id,
                credit_amount=Decimal("100.00"),
                foreign_currency_code="USD",
                foreign_amount=Decimal("100.00"),
            ),
        ],
    )
    settle_open_item(
        session=session, open_item_id=items[0].id, changed_by="t", journal_entry_id=payment.id
    )
    with pytest.raises(OpenItemError, match="schon zugeordnet"):
        settle_open_item(
            session=session, open_item_id=items[1].id, changed_by="t", journal_entry_id=payment.id
        )


def test_euro_item_on_a_foreign_payment_leaves_only_the_rest(session: Session) -> None:
    """Die Unterzahlung des USD-Postens ist kein Kursverlust, der Posten bleibt anteilig offen."""
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "188.89", day=date(2026, 3, 3))
    euro = _euro_item(session, company, accounts["1400"], "50.00")
    usd = _fx_item(session, company, accounts["1400"], foreign="150.00")
    transaction = _booked_usd_payment(session, company, accounts, "150.00", rate="1.08")
    settle_open_item(
        session=session,
        open_item_id=euro.id,
        changed_by="t",
        journal_entry_id=transaction.journal_entry_id,
    )
    settled = settle_open_item(
        session=session, open_item_id=usd.id, changed_by="t", bank_transaction_id=transaction.id
    )
    assert settled.status == "open"
    assert settled.foreign_open_amount == Decimal("54.00")
    row = session.execute(
        select(OpenItemSettlement).where(OpenItemSettlement.open_item_id == usd.id)
    ).scalar_one()
    assert row.paid_amount == Decimal("88.89")
    assert row.fx_difference == Decimal("0.00")
    assert _balance(session, accounts["2150"]) == Decimal("0.00")
    assert _balance(session, accounts["1400"]) == settled.open_amount


def test_old_settlement_row_is_reversed_through_its_bank_transaction(session: Session) -> None:
    """Zeilen von vor dieser Änderung kennen nur den Bankumsatz, nicht seine Buchung."""
    company, accounts = _seed(session)
    item = _euro_item(session, company, accounts["1400"], "100.00")
    transaction = _booked_bank_tx(session, company, accounts, "100.00", "EUR")
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    row = session.execute(select(OpenItemSettlement)).scalar_one()
    row.journal_entry_id = None
    session.commit()
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    session.refresh(item)
    session.refresh(row)
    assert item.status == "open"
    assert row.reversed_on == date(2026, 3, 25)


def test_part_payment_worth_less_than_a_cent_is_refused(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(
        session,
        company,
        accounts["1400"],
        foreign_currency_code="IDR",
        foreign="300.00",
        exchange_rate=Decimal("15000"),
    )
    assert item.original_amount == Decimal("0.02")
    with pytest.raises(OpenItemError, match="weniger als ein Cent"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            foreign_amount=Decimal("1.00"),
            amount=Decimal("0.01"),
        )
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        foreign_amount=Decimal("100.00"),
        amount=Decimal("0.01"),
    )
    assert settled.open_amount == Decimal("0.01")
    # Der letzte Cent bleibt für den Rest, der Posten lässt sich immer schließen.
    with pytest.raises(OpenItemError, match="weniger als ein Cent"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            foreign_amount=Decimal("100.00"),
            amount=Decimal("0.01"),
        )
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        foreign_amount=Decimal("200.00"),
        amount=Decimal("0.01"),
    )
    assert settled.status == "settled"


def test_inactive_partner_does_not_block_the_difference(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"], partner_id=customer.id)
    customer.is_active = False
    session.commit()
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1010.00")
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1010.00"),
    )
    assert settled.status == "settled"
    assert _balance(session, accounts["2660"]) == Decimal("-10.00")


def test_reversal_of_large_foreign_line_with_derived_rate_works(session: Session) -> None:
    """Der abgeleitete Kurs hat sechs Stellen: Große Beträge liegen mehr als einen Cent daneben."""
    company, accounts = _seed(session)
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 3, 3),
            description="Großer Betrag",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(
                    account_id=accounts["1210"].id,
                    debit_amount=Decimal("9259000.00"),
                    foreign_currency_code="USD",
                    foreign_amount=Decimal("10000000.00"),
                ),
                JournalLineInput(
                    account_id=accounts["8338"].id, credit_amount=Decimal("9259000.00")
                ),
            ],
        ),
    )
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 3, 4),
        changed_by="t",
    )
    assert reversal.reversal_of_id == entry.id


def test_payment_run_refuses_foreign_items(session: Session) -> None:
    company, accounts = _seed(session)
    item = _fx_item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        counterparty_iban="DE89370400440532013000",
    )
    assert payable_items_for_run(session=session, company_id=company.id) == []
    with pytest.raises(SepaExportError, match="nur in EUR"):
        create_payment_run(
            session=session, company_id=company.id, open_item_ids=[item.id], changed_by="t"
        )


def test_dunning_shows_the_foreign_amount(session: Session) -> None:
    company, accounts = _seed(session)
    _fx_item(session, company, accounts["1400"], due_date=date(2026, 3, 10))
    proposal = dunning_proposals(session=session, company_id=company.id)[0]
    payload = serialize_proposal(proposal)
    assert payload["foreign_currency_code"] == "USD"
    assert payload["foreign_open_amount"] == "1080.00"


def test_rate_is_rounded_like_the_column_before_sealing(session: Session) -> None:
    company, accounts = _seed(session)
    entry = _fx_entry(
        session,
        company,
        accounts,
        foreign_currency_code="USD",
        foreign_amount=Decimal("108.00"),
        exchange_rate=Decimal("1.0800004"),
    )
    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
    session.expire_all()
    assert verify_compliance_integrity(session=session, company_id=company.id).valid is True


# ---------------------------------------------------------------------------
# Bank
# ---------------------------------------------------------------------------


def _booked_bank_tx(session, company, accounts, amount, currency, *, rate=None, contra="1400"):
    bank = accounts["1210"] if currency == "USD" else accounts["1200"]
    transaction = _bank_tx(session, company, bank, amount, currency)
    return book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts[contra].id,
        changed_by="t",
        exchange_rate=Decimal(rate) if rate is not None else None,
    )


def _bank_tx(session, company, account, amount, currency, day=date(2026, 3, 3)) -> BankTransaction:
    transaction = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=account.id,
        booking_date=day,
        amount=Decimal(amount),
        currency_code=currency,
        purpose="Zahlung",
        status="open",
        dedup_hash=f"tx-{amount}-{currency}",
    )
    session.add(transaction)
    session.commit()
    return transaction


def test_foreign_bank_transaction_needs_rate_and_books_in_euro(session: Session) -> None:
    company, accounts = _seed(session)
    transaction = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    with pytest.raises(BankImportError, match="keinen Kurs"):
        book_transaction(
            session=session,
            transaction_id=transaction.id,
            contra_account_id=accounts["8338"].id,
            changed_by="t",
        )
    booked = book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["8338"].id,
        changed_by="t",
        exchange_rate=Decimal("1.08"),
    )
    entry = session.get(JournalEntry, booked.journal_entry_id)
    bank_line = next(line for line in entry.lines if line.account_id == accounts["1210"].id)
    assert bank_line.debit_amount == Decimal("100.00")
    assert bank_line.foreign_currency_code == "USD"
    assert bank_line.foreign_amount == Decimal("108.00")
    assert bank_line.exchange_rate == Decimal("1.080000")


def test_foreign_bank_transaction_uses_daily_rate_and_tax_in_euro(session: Session) -> None:
    company, accounts = _seed(session)
    _rate(session, company)
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    transaction = _bank_tx(session, company, accounts["1210"], "1285.20", "USD")
    booked = book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["8400"].id,
        tax_code_id=tax_code.id,
        changed_by="t",
    )
    entry = session.get(JournalEntry, booked.journal_entry_id)
    amounts = {line.account_id: line for line in entry.lines}
    assert amounts[accounts["1210"].id].debit_amount == Decimal("1190.00")
    assert amounts[accounts["8400"].id].credit_amount == Decimal("1000.00")
    assert amounts[tax_code.vat_account_id].credit_amount == Decimal("190.00")


def test_zero_rate_is_refused_instead_of_replaced_by_the_daily_rate(session: Session) -> None:
    company, accounts = _seed(session)
    _rate(session, company)
    transaction = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    with pytest.raises(BankImportError, match="größer 0"):
        book_transaction(
            session=session,
            transaction_id=transaction.id,
            contra_account_id=accounts["8338"].id,
            changed_by="t",
            exchange_rate=Decimal("0"),
        )


def test_reassigning_a_booked_foreign_transaction_moves_the_euro_amount(session: Session) -> None:
    company, accounts = _seed(session)
    transaction = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["8338"].id,
        changed_by="t",
        exchange_rate=Decimal("1.08"),
    )
    reassign_bank_transactions(
        session=session,
        transaction_ids=[transaction.id],
        bank_account_id=accounts["1200"].id,
        changed_by="t",
    )
    assert _balance(session, accounts["1210"]) == Decimal("0.00")
    assert _balance(session, accounts["1200"]) == Decimal("100.00")


def test_second_reassignment_moves_the_euro_amount_again(session: Session) -> None:
    company, accounts = _seed(session)
    third = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="1220",
        name="Bank 3",
        account_type="asset",
    )
    session.add(third)
    session.commit()
    transaction = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["8338"].id,
        changed_by="t",
        exchange_rate=Decimal("1.08"),
    )
    for target in (accounts["1200"], third):
        reassign_bank_transactions(
            session=session,
            transaction_ids=[transaction.id],
            bank_account_id=target.id,
            changed_by="t",
        )
    assert _balance(session, accounts["1210"]) == Decimal("0.00")
    assert _balance(session, accounts["1200"]) == Decimal("0.00")
    assert _balance(session, third) == Decimal("100.00")


def test_second_reassignment_of_a_match_without_foreign_amount(session: Session) -> None:
    company, accounts = _seed(session)
    third = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="1220",
        name="Bank 3",
        account_type="asset",
    )
    session.add(third)
    session.commit()
    entry = _book(
        session, company, accounts["1210"], accounts["8338"], "100.00", day=date(2026, 3, 3)
    )
    transaction = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    match_transaction(
        session=session, transaction_id=transaction.id, journal_entry_id=entry.id, changed_by="t"
    )
    for target in (accounts["1200"], third):
        reassign_bank_transactions(
            session=session,
            transaction_ids=[transaction.id],
            bank_account_id=target.id,
            changed_by="t",
        )
    assert _balance(session, accounts["1210"]) == Decimal("0.00")
    assert _balance(session, accounts["1200"]) == Decimal("0.00")
    assert _balance(session, third) == Decimal("100.00")


def _reconciliation(session, company) -> dict[str, tuple[Decimal, Decimal]]:
    return {
        row.account_code: (row.book_balance, row.statement_total)
        for row in bank_reconciliation(session=session, company_id=company.id)
    }


def test_reassigning_foreign_transaction_keeps_the_reconciliation(session: Session) -> None:
    company, accounts = _seed(session)
    for bank, amount in ((accounts["1200"], "10.00"), (accounts["1210"], "20.00")):
        book_transaction(
            session=session,
            transaction_id=_bank_tx(session, company, bank, amount, "EUR").id,
            contra_account_id=accounts["8338"].id,
            changed_by="t",
        )
    expected = {
        "1200": (Decimal("10.00"), Decimal("10.00")),
        "1210": (Decimal("20.00"), Decimal("20.00")),
    }
    assert _reconciliation(session, company) == expected
    usd = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    book_transaction(
        session=session,
        transaction_id=usd.id,
        contra_account_id=accounts["8338"].id,
        changed_by="t",
        exchange_rate=Decimal("1.08"),
    )
    assert _reconciliation(session, company) == expected
    reassign_bank_transactions(
        session=session,
        transaction_ids=[usd.id],
        bank_account_id=accounts["1200"].id,
        changed_by="t",
    )
    assert _reconciliation(session, company) == expected
    assert _balance(session, accounts["1200"]) == Decimal("110.00")
    assert _balance(session, accounts["1210"]) == Decimal("20.00")


def test_euro_transaction_with_foreign_note_on_its_entry_counts(session: Session) -> None:
    """Der Eurobetrag steht im Auszug; die Buchung nennt nur zusätzlich die Fremdwährung."""
    company, accounts = _seed(session)
    transaction = _bank_tx(session, company, accounts["1200"], "92.59", "EUR")
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 3, 3),
            description="Zahlung",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(
                    account_id=accounts["1200"].id,
                    debit_amount=Decimal("92.59"),
                    foreign_currency_code="USD",
                    foreign_amount=Decimal("100.00"),
                ),
                JournalLineInput(account_id=accounts["8338"].id, credit_amount=Decimal("92.59")),
            ],
        ),
    )
    match_transaction(
        session=session, transaction_id=transaction.id, journal_entry_id=entry.id, changed_by="t"
    )
    assert _reconciliation(session, company)["1200"] == (Decimal("92.59"), Decimal("92.59"))


def test_foreign_transactions_stay_out_of_euro_comparisons(session: Session) -> None:
    company, accounts = _seed(session)
    # Eine Buchung über 108 EUR passt nicht zu einem Umsatz über 108 USD.
    _book(session, company, accounts["1210"], accounts["8338"], "108.00", day=date(2026, 3, 3))
    usd = _bank_tx(session, company, accounts["1210"], "108.00", "USD")
    assert suggest_matches(session=session, transaction=usd) == []

    # Gleiche Zahl, andere Währung: kein Eigenübertrag.
    euro_out = _bank_tx(session, company, accounts["1200"], "-108.00", "EUR")
    assert detect_transfer_counterparts(session=session, transactions=[usd, euro_out]) == {}
    usd_out = _bank_tx(session, company, accounts["1200"], "-108.00", "USD")
    found = detect_transfer_counterparts(session=session, transactions=[usd, usd_out])
    assert found[usd.id].id == usd_out.id

    # Saldenabgleich: Weder die Zeile noch der Umsatz in USD zählen.
    book_transaction(
        session=session,
        transaction_id=usd.id,
        contra_account_id=accounts["8338"].id,
        changed_by="t",
        exchange_rate=Decimal("1.08"),
    )
    row = next(
        row
        for row in bank_reconciliation(session=session, company_id=company.id)
        if row.account_id == accounts["1200"].id
    )
    assert row.statement_total == Decimal("-108.00")
    assert row.book_balance == Decimal("0.00")
    rows = {row["account_id"]: row for row in bank_account_rows(session, company.id)}
    assert rows[accounts["1210"].id]["statement_total"] == Decimal("0.00")
    assert rows[accounts["1200"].id]["statement_total"] == Decimal("-108.00")


def test_payment_on_foreign_bank_transaction_settles_foreign_item(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"], foreign="540.00")
    item_id = item.id
    transaction = _booked_usd_payment(session, company, accounts, "270.00", rate="1.08")
    settled = settle_open_item(
        session=session,
        open_item_id=item_id,
        changed_by="t",
        bank_transaction_id=transaction.id,
    )
    assert settled.foreign_open_amount == Decimal("270.00")
    assert settled.status == "open"


# ---------------------------------------------------------------------------
# Stichtagsbewertung
# ---------------------------------------------------------------------------


def test_valuation_books_differences_and_reverses_next_day(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    supplier = create_partner(
        session=session, company=company, changed_by="t", name="Lieferant", is_supplier=True
    )
    session.commit()
    _fx_item(session, company, accounts["1400"], partner_id=customer.id)
    _fx_item(
        session,
        company,
        accounts["1600"],
        item_type="payable",
        reference="FX-K",
        partner_id=supplier.id,
    )
    as_of = date(2026, 12, 31)
    report = fx_valuation_report(session=session, company=company, as_of=as_of)
    assert report["missing_rates"] == ["USD"]
    with pytest.raises(ExchangeRateError, match="fehlen Kurse"):
        post_fx_valuation(session=session, company=company, as_of=as_of, changed_by="t")

    # Der Euro wird stärker: 1.080 USD sind weniger wert.
    _rate(session, company, day=as_of, rate="1.20")
    report = fx_valuation_report(session=session, company=company, as_of=as_of)
    rows = {row.item.reference: row for row in report["rows"]}
    assert rows["FX-1"].value == Decimal("900.00")
    assert rows["FX-1"].difference == Decimal("-100.00")
    assert rows["FX-K"].difference == Decimal("100.00")
    assert report["total_gain"] == Decimal("100.00")
    assert report["total_loss"] == Decimal("-100.00")

    result = post_fx_valuation(session=session, company=company, as_of=as_of, changed_by="t")
    assert result["gain"] == Decimal("100.00")
    assert result["loss"] == Decimal("100.00")
    entry = session.get(JournalEntry, result["journal_entry_id"])
    assert entry.entry_date == as_of
    assert entry.source == "fx_valuation"
    collective = {line.account_id: line for line in entry.lines}
    assert collective[accounts["1400"].id].credit_amount == Decimal("100.00")
    assert collective[accounts["1400"].id].partner_id == customer.id
    assert collective[accounts["1600"].id].debit_amount == Decimal("100.00")
    assert collective[accounts["2150"].id].debit_amount == Decimal("100.00")
    assert collective[accounts["2660"].id].credit_amount == Decimal("100.00")

    reversal = session.get(JournalEntry, result["reversal_entry_id"])
    assert reversal.entry_date == date(2027, 1, 1)
    assert _balance(session, accounts["1400"]) == Decimal("0.00")

    # Die Posten behalten ihren Buchwert, doppelt buchen geht nicht.
    assert session.execute(
        select(OpenItem.open_amount).where(OpenItem.reference == "FX-1")
    ).scalar_one() == Decimal("1000.00")
    with pytest.raises(ExchangeRateError, match="bereits gebucht"):
        post_fx_valuation(session=session, company=company, as_of=as_of, changed_by="t")
    assert (
        fx_valuation_report(session=session, company=company, as_of=as_of)["posted_entry_id"]
        == entry.id
    )


def test_valuation_books_only_losses_for_long_term_items(session: Session) -> None:
    company, accounts = _seed(session)
    _fx_item(session, company, accounts["1400"], due_date=date(2028, 6, 30), reference="LANG")
    _rate(session, company, day=date(2026, 12, 31), rate="1.00")
    report = fx_valuation_report(session=session, company=company, as_of=date(2026, 12, 31))
    row = report["rows"][0]
    assert row.long_term is True
    assert row.difference == Decimal("80.00")
    assert row.booked_difference == Decimal("0.00")
    with pytest.raises(ExchangeRateError, match="keine Kursdifferenz"):
        post_fx_valuation(
            session=session, company=company, as_of=date(2026, 12, 31), changed_by="t"
        )

    _rate(session, company, day=date(2026, 12, 31), rate="1.20")
    report = fx_valuation_report(session=session, company=company, as_of=date(2026, 12, 31))
    assert report["rows"][0].booked_difference == Decimal("-100.00")


def test_valuation_takes_the_state_on_the_reporting_date(session: Session) -> None:
    company, accounts = _seed(session)
    as_of = date(2026, 12, 31)
    _rate(session, company, day=as_of, rate="1.20")
    paid_later = _fx_item(session, company, accounts["1400"], reference="SPAETER")
    paid_before = _fx_item(session, company, accounts["1400"], reference="VORHER")
    partly_paid = _fx_item(session, company, accounts["1400"], reference="TEIL")
    settle_open_item(
        session=session,
        open_item_id=paid_later.id,
        changed_by="t",
        exchange_rate=Decimal("1.10"),
        settlement_date=date(2027, 1, 20),
    )
    settle_open_item(
        session=session,
        open_item_id=paid_before.id,
        changed_by="t",
        exchange_rate=Decimal("1.10"),
        settlement_date=date(2026, 12, 20),
    )
    settle_open_item(
        session=session,
        open_item_id=partly_paid.id,
        changed_by="t",
        foreign_amount=Decimal("540.00"),
        exchange_rate=Decimal("1.10"),
        settlement_date=date(2026, 12, 20),
    )
    report = fx_valuation_report(session=session, company=company, as_of=as_of)
    rows = {row.item.reference: row for row in report["rows"]}
    assert set(rows) == {"SPAETER", "TEIL"}
    assert rows["SPAETER"].foreign_open == Decimal("1080.00")
    assert rows["SPAETER"].carrying == Decimal("1000.00")
    assert rows["SPAETER"].difference == Decimal("-100.00")
    assert rows["TEIL"].foreign_open == Decimal("540.00")
    assert rows["TEIL"].carrying == Decimal("500.00")
    assert rows["TEIL"].difference == Decimal("-50.00")
    # Vor dem Zahlungsdatum auch der früher bezahlte Posten.
    _rate(session, company, day=date(2026, 12, 10), rate="1.20")
    earlier = fx_valuation_report(session=session, company=company, as_of=date(2026, 12, 10))
    assert {row.item.reference for row in earlier["rows"]} == {"SPAETER", "VORHER", "TEIL"}


def test_rebooked_payment_counts_once_on_the_reporting_date(session: Session) -> None:
    company, accounts = _seed(session)
    _rate(session, company, day=date(2026, 3, 31), rate="1.00")
    _rate(session, company, day=date(2026, 4, 30), rate="1.00")
    item = _fx_item(
        session, company, accounts["1400"], foreign="1000.00", exchange_rate=Decimal("1")
    )
    transaction = _booked_usd_payment(session, company, accounts, "400.00")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=transaction.id,
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2026, 4, 5),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
        exchange_rate=Decimal("1.00"),
    )
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=transaction.id,
    )
    latest = session.execute(
        select(OpenItemSettlement).where(OpenItemSettlement.reversed_on.is_(None))
    ).scalar_one()
    assert latest.settlement_date == date(2026, 4, 5)
    for as_of in (date(2026, 3, 31), date(2026, 4, 30)):
        rows = fx_valuation_report(session=session, company=company, as_of=as_of)["rows"]
        assert [row.foreign_open for row in rows] == [Decimal("600.00")]


def test_reversal_of_another_payment_keeps_the_date(session: Session) -> None:
    company, accounts = _seed(session)
    _rate(session, company, day=date(2026, 3, 31), rate="1.00")
    item = _fx_item(
        session, company, accounts["1400"], foreign="1000.00", exchange_rate=Decimal("1")
    )
    bank, collective = accounts["1200"], accounts["1400"]
    first = _book(session, company, bank, collective, "300.00", day=date(2026, 3, 10))
    second = _book(session, company, bank, collective, "200.00", day=date(2026, 3, 20))
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=first.id,
        foreign_amount=Decimal("300.00"),
        amount=Decimal("300.00"),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=first.id,
        reversal_date=date(2026, 4, 5),
        changed_by="t",
    )
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=second.id,
        foreign_amount=Decimal("200.00"),
        amount=Decimal("200.00"),
    )
    latest = session.execute(
        select(OpenItemSettlement).where(OpenItemSettlement.reversed_on.is_(None))
    ).scalar_one()
    assert latest.settlement_date == date(2026, 3, 20)
    # Zum 31.03. waren beide Zahlungen gebucht.
    rows = fx_valuation_report(session=session, company=company, as_of=date(2026, 3, 31))["rows"]
    assert [row.foreign_open for row in rows] == [Decimal("500.00")]


def test_valuation_respects_reversed_settlements_and_cancellations(session: Session) -> None:
    company, accounts = _seed(session)
    as_of = date(2026, 3, 31)
    _rate(session, company, day=as_of, rate="1.20")
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1000.00")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=payment.id,
        foreign_amount=Decimal("1080.00"),
        amount=Decimal("1000.00"),
    )
    assert fx_valuation_report(session=session, company=company, as_of=as_of)["rows"] == []
    reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        reversal_date=date(2026, 3, 25),
        changed_by="t",
    )
    rows = fx_valuation_report(session=session, company=company, as_of=as_of)["rows"]
    assert [row.foreign_open for row in rows] == [Decimal("1080.00")]
    # Vor dem Storno galt der Posten als bezahlt.
    _rate(session, company, day=date(2026, 3, 22), rate="1.20")
    assert (
        fx_valuation_report(session=session, company=company, as_of=date(2026, 3, 22))["rows"]
        == []
    )


def test_valuation_does_not_net_gains_and_losses_of_one_partner(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    as_of = date(2026, 12, 31)
    _rate(session, company, day=as_of, rate="1.20")
    _rate(session, company, day=as_of, rate="0.80", currency="CHF")
    _fx_item(session, company, accounts["1400"], partner_id=customer.id)
    _fx_item(
        session,
        company,
        accounts["1400"],
        reference="FX-CHF",
        partner_id=customer.id,
        foreign_currency_code="CHF",
        foreign="1000.00",
        exchange_rate=Decimal("1.00"),
    )
    result = post_fx_valuation(session=session, company=company, as_of=as_of, changed_by="t")
    assert result["gain"] == Decimal("250.00")
    assert result["loss"] == Decimal("100.00")
    entry = session.get(JournalEntry, result["journal_entry_id"])
    collective = sorted(
        (line.debit_amount, line.credit_amount)
        for line in entry.lines
        if line.account_id == accounts["1400"].id
    )
    assert collective == [
        (Decimal("0.00"), Decimal("100.00")),
        (Decimal("250.00"), Decimal("0.00")),
    ]


def test_valuation_books_for_an_inactive_partner(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    as_of = date(2026, 12, 31)
    _rate(session, company, day=as_of, rate="1.20")
    _fx_item(session, company, accounts["1400"], partner_id=customer.id)
    customer.is_active = False
    session.commit()
    result = post_fx_valuation(session=session, company=company, as_of=as_of, changed_by="t")
    assert result["loss"] == Decimal("100.00")


# ---------------------------------------------------------------------------
# Rechnung
# ---------------------------------------------------------------------------


def _customer(session, company):
    partner = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="US Customer Inc.",
        is_customer=True,
        street="Main St. 1",
        postal_code="10001",
        city="New York",
        country_code="US",
    )
    session.commit()
    return partner


def _usd_invoice(session, company, accounts, upload_dir, **fields):
    fields.setdefault("service_date", date(2026, 3, 3))
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    draft = create_sales_invoice(
        session=session,
        company=company,
        changed_by="t",
        partner_id=_customer(session, company).id,
        issue_date=date(2026, 3, 3),
        currency_code="USD",
        lines=[
            {
                "description": "Leistung A",
                "quantity": "1",
                "unit_price": "1000.00",
                "tax_code_id": tax_code.id,
                "revenue_account_id": accounts["8400"].id,
            },
            {
                "description": "Leistung B",
                "quantity": "3",
                "unit_price": "33.33",
                "tax_code_id": tax_code.id,
                "revenue_account_id": accounts["8338"].id,
            },
        ],
        **fields,
    )
    return issue_sales_invoice(
        session=session, invoice_id=draft.id, changed_by="t", upload_dir=upload_dir
    )


def test_invoice_in_foreign_currency_books_euro_and_keeps_foreign_item(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    issued = _usd_invoice(
        session, company, accounts, tmp_path, exchange_rate="1.08"
    )
    assert issued.currency_code == "USD"
    assert issued.exchange_rate == Decimal("1.080000")
    assert issued.gross_total == Decimal("1308.99")

    entry = session.get(JournalEntry, issued.journal_entry_id)
    receivable = next(line for line in entry.lines if line.account_id == accounts["1400"].id)
    assert receivable.debit_amount == Decimal("1212.03")
    assert receivable.foreign_currency_code == "USD"
    assert receivable.foreign_amount == Decimal("1308.99")
    assert sum(line.debit_amount for line in entry.lines) == sum(
        line.credit_amount for line in entry.lines
    )

    item = issued.open_item
    assert item.original_amount == Decimal("1212.03")
    assert item.foreign_original_amount == Decimal("1308.99")
    assert item.foreign_currency_code == "USD"


def test_invoice_without_rate_uses_daily_rate_or_lists_the_problem(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    with pytest.raises(SalesInvoiceError) as raised:
        _usd_invoice(session, company, accounts, tmp_path)
    assert any("keinen Kurs" in problem for problem in raised.value.problems)
    session.rollback()

    _rate(session, company)
    issued = _usd_invoice(session, company, accounts, tmp_path)
    assert issued.exchange_rate == Decimal("1.080000")


def test_invoice_rate_follows_the_service_date(session: Session, tmp_path: Path) -> None:
    company, accounts = _seed(session)
    _rate(session, company, day=date(2026, 3, 3), rate="1.0800")
    _rate(session, company, day=date(2026, 2, 20), rate="1.1000")
    # Die Leistung lag im Februar: Ihr Kurs gilt, nicht der des Rechnungsdatums.
    issued = _usd_invoice(session, company, accounts, tmp_path, service_date=date(2026, 2, 20))
    assert issued.exchange_rate == Decimal("1.100000")
    # Eine Leistung nach dem Rechnungsdatum hat noch keinen Kurs.
    later = _usd_invoice(session, company, accounts, tmp_path, service_date=date(2026, 3, 25))
    assert later.exchange_rate == Decimal("1.080000")


def test_currency_change_drops_the_rate_of_the_old_currency(session: Session) -> None:
    company, accounts = _seed(session)
    draft = create_sales_invoice(
        session=session,
        company=company,
        changed_by="t",
        partner_id=_customer(session, company).id,
        currency_code="USD",
        exchange_rate="1.08",
    )
    update_sales_invoice(session=session, invoice=draft, changed_by="t", currency_code="CHF")
    assert draft.exchange_rate is None
    update_sales_invoice(
        session=session, invoice=draft, changed_by="t", currency_code="USD", exchange_rate="1.10"
    )
    assert draft.exchange_rate == Decimal("1.10")
    update_sales_invoice(session=session, invoice=draft, changed_by="t", due_date="2026-04-30")
    assert draft.exchange_rate == Decimal("1.10")


def test_paid_foreign_invoice_names_euro_and_foreign_rest(session: Session, tmp_path: Path) -> None:
    company, accounts = _seed(session)
    _rate(session, company)
    issued = _usd_invoice(session, company, accounts, tmp_path)
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        foreign_amount=Decimal("100.00"),
        exchange_rate=Decimal("1.08"),
    )
    session.refresh(issued)
    payload = serialize_sales_invoice(issued)
    assert payload["foreign_open_amount"] == str(issued.open_item.foreign_open_amount)
    with pytest.raises(SalesInvoiceStateError, match=r"92,59 EUR"):
        cancel_sales_invoice(
            session=session,
            invoice_id=issued.id,
            changed_by="t",
            cancellation_date=date(2026, 3, 10),
            reason="Falsch",
            upload_dir=tmp_path,
        )


def test_cash_basis_names_a_missing_rate_of_the_payment_month(
    session: Session, tmp_path: Path
) -> None:
    """Die Rechnung zählt zum Monatskurs der Zahlung; ohne ihn nennt die UStVA, was fehlt."""
    company, accounts = _seed(session)
    set_company_vat_method(
        session=session,
        company=company,
        method="ist",
        valid_from=date(2026, 1, 1),
        changed_by="t",
    )
    _rate(session, company)
    issued = _usd_invoice(session, company, accounts, tmp_path)
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        exchange_rate=Decimal("1.08"),
        settlement_date=date(2026, 4, 10),
    )
    march = compute_vat_return_details(
        session=session,
        company_id=company.id,
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 31),
    )
    assert not any("USD 2026-04" in warning for warning in march.warnings)
    april = compute_vat_return_details(
        session=session,
        company_id=company.id,
        date_from=date(2026, 4, 1),
        date_to=date(2026, 4, 30),
    )
    assert any("USD 2026-04" in warning for warning in april.warnings)


def test_foreign_einvoice_is_read_correctly_and_not_booked_as_euro(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _rate(session, company)
    for syntax in ("cii", "ubl"):
        issued = _usd_invoice(session, company, accounts, tmp_path, einvoice_syntax=syntax)
        parsed = parse_einvoice(_einvoice_xml(session, issued))
        # Auch wenn in CII zuletzt die Steuer in Euro steht (BT-111).
        assert parsed.currency_code == "USD"
        with pytest.raises(IncomingInvoiceError, match="läuft in USD") as raised:
            book_einvoice(
                session=session,
                company=company,
                invoice=parsed,
                expense_account_id=accounts["4210"].id,
                creditor_account_id=accounts["1600"].id,
                changed_by="t",
            )
        assert raised.value.code == "foreign_currency"


def _einvoice_xml(session, invoice) -> bytes:
    document = session.get(Document, invoice.xml_document_id)
    return Path(document.storage_key).read_bytes()


def _pdf_of(session, invoice) -> bytes:
    document = session.get(Document, invoice.pdf_document_id)
    return Path(document.storage_key).read_bytes()


def test_foreign_invoice_output_states_rate_and_tax_in_euro(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    issued = _usd_invoice(session, company, accounts, tmp_path, exchange_rate="1.08")
    xml = _einvoice_xml(session, issued)
    root = ET.fromstring(xml)

    def local(name: str):
        return [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == name]

    assert local("DocumentCurrencyCode")[0].text == "USD"
    assert local("TaxCurrencyCode")[0].text == "EUR"
    tax_totals = [el for el in local("TaxAmount") if el.get("currencyID") == "EUR"]
    assert [el.text for el in tax_totals] == ["193.52"]
    notes = " ".join(el.text or "" for el in local("Note"))
    assert "1 EUR = 1,08 USD" in notes
    assert "193,52 EUR" in notes
    text_of_pdf = pdf_text(_pdf_of(session, issued))
    assert "1 EUR = 1,08 USD" in text_of_pdf
    assert "193,52 EUR" in text_of_pdf


def test_cancelling_foreign_invoice_keeps_rate_and_closes_item(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    issued = _usd_invoice(session, company, accounts, tmp_path, exchange_rate="1.08")
    cancellation = cancel_sales_invoice(
        session=session,
        invoice_id=issued.id,
        changed_by="t",
        upload_dir=tmp_path,
        cancellation_date=date(2026, 3, 10),
    )
    assert cancellation.exchange_rate == Decimal("1.080000")
    session.expire_all()
    item = session.execute(select(OpenItem)).scalar_one()
    assert item.status == "settled"
    assert item.foreign_open_amount == Decimal("0.00")
    assert _balance(session, accounts["1400"]) == Decimal("0.00")


# ---------------------------------------------------------------------------
# Prüferexport, REST, Oberfläche, Migration
# ---------------------------------------------------------------------------


def test_exchange_rates_are_in_the_audit_export() -> None:
    assert EXPORT_TABLE_MODELS["exchange_rates"] is ExchangeRate


def _api_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "API_REQUIRE_AUTH": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'fx.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db:
        company, accounts = _seed(db)
        foreign_tenant = Tenant(name="Fremd")
        foreign = Company(tenant=foreign_tenant, name="Fremd GmbH", currency_code="EUR")
        db.add_all([foreign_tenant, foreign])
        db.flush()
        for username, role, token in (
            ("pruefer", "Pruefer", "obk_pruefer-fx"),
            ("buchhalter", "Buchhalter", "obk_buchhalter-fx"),
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
        ids = {code: account.id for code, account in accounts.items()}
        return app, company.id, foreign.id, ids


READER = {"Authorization": "Bearer obk_pruefer-fx"}
WRITER = {"Authorization": "Bearer obk_buchhalter-fx"}


def test_api_covers_rates_items_journal_and_valuation(tmp_path: Path) -> None:
    app, company_id, foreign_id, ids = _api_app(tmp_path)
    client = app.test_client()

    base = {"company_id": company_id, "currency_code": "USD", "rate_date": "2026-03-03"}
    url = "/api/v1/exchange-rates"
    assert client.post(url, headers=READER, json={**base, "rate": "1.08"}).status_code == 403
    assert client.post(url, headers=WRITER, json={**base, "rate": "0"}).status_code == 422
    assert (
        client.post(url, headers=WRITER, json={**base, "rate_date": "x", "rate": "1"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/exchange-rates",
            headers=WRITER,
            json={**base, "company_id": foreign_id, "rate": "1.08"},
        ).status_code
        == 404
    )
    created = client.post("/api/v1/exchange-rates", headers=WRITER, json={**base, "rate": "1.08"})
    assert created.status_code == 201
    assert created.get_json()["rate"] == "1.080000"
    listed = client.get(
        "/api/v1/exchange-rates",
        headers=READER,
        query_string={"company_id": company_id, "currency_code": "USD"},
    ).get_json()["exchange_rates"]
    assert [row["rate_date"] for row in listed] == ["2026-03-03"]

    item = client.post(
        "/api/v1/open-items",
        headers=WRITER,
        json={
            "company_id": company_id,
            "account_id": ids["1400"],
            "item_type": "receivable",
            "reference": "FX-API",
            "entry_date": "2026-03-03",
            "foreign_currency_code": "USD",
            "foreign_amount": "1080.00",
        },
    )
    assert item.status_code == 201, item.get_json()
    payload = item.get_json()
    assert payload["original_amount"] == "1000.00"
    assert payload["foreign_open_amount"] == "1080.00"
    assert payload["exchange_rate"] == "1.080000"
    assert (
        client.post(
            "/api/v1/open-items",
            headers=WRITER,
            json={
                "company_id": company_id,
                "account_id": ids["1400"],
                "item_type": "receivable",
                "reference": "OHNE",
            },
        ).status_code
        == 400
    )

    booking = client.post(
        "/api/v1/journal-entries",
        headers=WRITER,
        json={
            "company_id": company_id,
            "entry_date": "2026-03-03",
            "description": "USD-Rechnung",
            "lines": [
                {
                    "account_id": ids["1400"],
                    "debit_amount": "1000.00",
                    "credit_amount": "0.00",
                    "foreign_currency_code": "USD",
                    "foreign_amount": "1080.00",
                    "exchange_rate": "1.08",
                },
                {"account_id": ids["8338"], "debit_amount": "0.00", "credit_amount": "1000.00"},
            ],
        },
    )
    assert booking.status_code == 201, booking.get_json()
    entries = client.get(
        "/api/v1/journal-entries", headers=READER, query_string={"company_id": company_id}
    ).get_json()["entries"]
    line = next(
        line
        for entry in entries
        for line in entry["lines"]
        if line["foreign_currency_code"]
    )
    assert line["foreign_amount"] == "1080.00"

    csv_text = client.get(
        "/api/v1/exports/journal.csv", headers=READER, query_string={"company_id": company_id}
    ).get_data(as_text=True)
    assert "foreign_currency_code" in csv_text
    assert "USD,1080.00" in csv_text

    settled = client.post(
        f"/api/v1/open-items/{payload['id']}/settle",
        headers=WRITER,
        json={"foreign_amount": "540.00", "exchange_rate": "1.08"},
    )
    assert settled.status_code == 200, settled.get_json()
    assert settled.get_json()["foreign_open_amount"] == "540.00"
    assert settled.get_json()["open_amount"] == "500.00"

    assert (
        client.post(
            "/api/v1/exchange-rates",
            headers=WRITER,
            json={**base, "rate_date": "2026-12-31", "rate": "1.20"},
        ).status_code
        == 201
    )
    report = client.get(
        "/api/v1/fx-valuation",
        headers=READER,
        query_string={"company_id": company_id, "as_of": "2026-12-31"},
    ).get_json()
    assert report["rows"][0]["booked_difference"] == "-50.00"
    assert (
        client.get(
            "/api/v1/fx-valuation",
            headers=READER,
            query_string={"company_id": company_id, "as_of": "kaputt"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/fx-valuation",
            headers=READER,
            json={"company_id": company_id, "as_of": "2026-12-31"},
        ).status_code
        == 403
    )
    posted = client.post(
        "/api/v1/fx-valuation",
        headers=WRITER,
        json={"company_id": company_id, "as_of": "2026-12-31"},
    )
    assert posted.status_code == 201, posted.get_json()
    assert posted.get_json()["loss"] == "50.00"
    assert (
        client.post(
            "/api/v1/fx-valuation",
            headers=WRITER,
            json={"company_id": company_id, "as_of": "2026-12-31"},
        ).status_code
        == 422
    )


def test_ui_pages_show_foreign_currency(tmp_path: Path) -> None:
    app, company_id, _, ids = _api_app(tmp_path)
    client = app.test_client()
    assert (
        client.post(
            "/auth/login", data={"username": "buchhalter", "password": "geheim123"}
        ).status_code
        == 302
    )
    page = client.get("/fremdwaehrung", query_string={"company_id": company_id})
    assert page.status_code == 200
    assert "Wechselkurse" in page.get_data(as_text=True)
    assert "Keine offenen Posten in Fremdwährung" in page.get_data(as_text=True)

    assert (
        client.post(
            "/fremdwaehrung/kurs",
            data={
                "company_id": company_id,
                "currency_code": "USD",
                "rate_date": "2026-03-03",
                "rate": "1,08",
            },
        ).status_code
        == 302
    )
    assert (
        client.post(
            "/offene-posten",
            data={
                "company_id": company_id,
                "account_id": ids["1400"],
                "item_type": "receivable",
                "reference": "FX-UI",
                "entry_date": "2026-03-03",
                "foreign_currency_code": "USD",
                "foreign_amount": "108.00",
            },
        ).status_code
        == 302
    )
    items = client.get("/offene-posten", query_string={"company_id": company_id}).get_data(
        as_text=True
    )
    assert "FX-UI" in items
    assert "108,00 USD" in items
    page = client.get(
        "/fremdwaehrung", query_string={"company_id": company_id, "as_of": "2026-03-03"}
    ).get_data(as_text=True)
    assert "FX-UI" in page
    assert "1,08" in page
    assert "1.080000" not in page


def test_migration_round_trip_keeps_seals_valid(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'fx-migration.db'}"
    session_factory = create_session_factory(url)
    with session_factory() as session:
        company, accounts = _seed(session)
        entry = _fx_entry(
            session,
            company,
            accounts,
            foreign_currency_code="USD",
            foreign_amount=Decimal("108.00"),
        )
        finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
        company_id = company.id
        entry_id = entry.id
    engine = create_engine(url)
    config = _alembic_config(engine)

    command.downgrade(config, "20261103_0063")
    assert "exchange_rate" not in inspect(engine).get_table_names()
    assert "foreign_amount" not in {
        column["name"] for column in inspect(engine).get_columns("journal_entry_line")
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT content_hash_version FROM journal_entry WHERE id = :id"),
                {"id": entry_id},
            ).scalar_one()
            == 4
        )

    command.upgrade(config, "head")
    with Session(engine) as session:
        assert verify_compliance_integrity(session=session, company_id=company_id).valid is True
        with pytest.raises(Exception, match="immutable"):
            session.execute(
                text(
                    "UPDATE journal_entry_line SET foreign_amount = 1 "
                    "WHERE journal_entry_id = :id"
                ),
                {"id": entry_id},
            )
