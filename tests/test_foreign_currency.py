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
from app.services.bank_import import BankImportError, book_transaction
from app.services.compliance_integrity import (
    JOURNAL_CONTENT_HASH_VERSION,
    verify_compliance_integrity,
)
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
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entry,
    reverse_journal_entry,
)
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    settle_open_item,
)
from app.services.partners import create_partner
from app.services.sales_invoice_pdf import pdf_text
from app.services.sales_invoices import (
    SalesInvoiceError,
    cancel_sales_invoice,
    create_sales_invoice,
    issue_sales_invoice,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    Document,
    ExchangeRate,
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
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
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
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
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
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
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
    settle_open_item(session=session, open_item_id=item.id, changed_by="t", amount=Decimal("990"))
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
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )


def test_reversing_the_payment_restores_item_and_difference(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"])
    payment = _book(session, company, accounts["1200"], accounts["1400"], "1028.57")
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
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


# ---------------------------------------------------------------------------
# Bank
# ---------------------------------------------------------------------------


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


def test_payment_on_foreign_bank_transaction_settles_foreign_item(session: Session) -> None:
    company, accounts = _seed(session)
    _book(session, company, accounts["1400"], accounts["8338"], "1000.00", day=date(2026, 3, 3))
    item = _fx_item(session, company, accounts["1400"], foreign="540.00")
    item_id = item.id
    transaction = _bank_tx(session, company, accounts["1210"], "270.00", "USD")
    settled = settle_open_item(
        session=session,
        open_item_id=item_id,
        changed_by="t",
        bank_transaction_id=transaction.id,
        exchange_rate=Decimal("1.08"),
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
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    draft = create_sales_invoice(
        session=session,
        company=company,
        changed_by="t",
        partner_id=_customer(session, company).id,
        issue_date=date(2026, 3, 3),
        service_date=date(2026, 3, 3),
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
    assert "1.080000" in page


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
