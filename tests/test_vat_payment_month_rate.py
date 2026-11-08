"""Istversteuerung in Fremdwährung: Umsatzsteuer zum Monatskurs des Geldeingangs und BMF-Kurse.

Die Beträge sind von Hand nachgerechnet und stehen deshalb als Zahlen im Test. Grundfall: 1.000,00
USD netto, 190,00 USD Umsatzsteuer, 1.190,00 USD brutto, Rechnungskurs 1,25, also 800,00 € netto,
152,00 € Umsatzsteuer und 952,00 € brutto. Monatskurse: Februar 1,20, März 1,30, April und Mai 1,10.
"""

from __future__ import annotations

import http.client
import io
import json
import random
import urllib.error
import zipfile
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, create_session_factory
from app.services.audit_export import EXPORT_TABLE_MODELS, build_audit_export_package
from app.services.bank_import import book_transaction
from app.services.datev_export import build_datev_export
from app.services.exchange_rates import (
    ExchangeRateError,
    fetch_bmf_rates,
    find_exchange_rate,
    fx_valuation_rows,
    list_vat_exchange_rates,
    normalize_rate_month,
    parse_bmf_rates,
    set_exchange_rate,
    set_vat_exchange_rate,
)
from app.services.journal_entries import (
    SOURCE_VAT_FX_DIFFERENCE,
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    amend_journal_entry,
    create_journal_entry,
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
from app.services.sales_invoices import create_sales_invoice, issue_sales_invoice
from app.services.tax_codes import ensure_default_tax_codes
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return_details,
    period_bounds,
    save_vat_return,
    set_company_vat_method,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    JournalEntry,
    JournalEntryLine,
    OpenItemSettlement,
    TaxCode,
    Tenant,
    User,
    VatExchangeRate,
    VatReturn,
)

ZERO = Decimal("0.00")
RATES = {"2027-02": "1.20", "2027-03": "1.30", "2027-04": "1.10", "2027-05": "1.10"}


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session, *, cash_basis: bool = True) -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name="Monatskurs Mandant")
    company = Company(
        tenant=tenant,
        name="Monatskurs GmbH",
        currency_code="EUR",
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
        ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
        ("1576", "Abziehbare Vorsteuer 19 %", "asset", None),
        ("1776", "Umsatzsteuer 19 %", "liability", None),
        ("1771", "Umsatzsteuer 7 %", "liability", None),
        ("1571", "Abziehbare Vorsteuer 7 %", "asset", None),
        ("2150", "Aufwendungen aus Kursdifferenzen", "expense", None),
        ("2660", "Erträge aus Kursdifferenzen", "income", None),
        ("8400", "Erlöse 19 % USt", "income", None),
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
    if cash_basis:
        set_company_vat_method(
            session=session,
            company=company,
            method="ist",
            valid_from=date(2027, 1, 1),
            changed_by="t",
        )
    return company, accounts


def _monthly_rates(session, company, rates=None, currency="USD"):
    for month, rate in (RATES if rates is None else rates).items():
        set_vat_exchange_rate(
            session=session,
            company=company,
            currency_code=currency,
            month=month,
            rate=rate,
            changed_by="t",
        )


def _invoice(
    session,
    company,
    accounts,
    upload_dir,
    *,
    currency="USD",
    rate="1.25",
    net="1000.00",
    day=date(2027, 1, 15),
):
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    partner = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name=f"Kunde {net} {currency}",
        is_customer=True,
        street="Main St. 1",
        postal_code="10001",
        city="New York",
        country_code="US",
    )
    session.commit()
    draft = create_sales_invoice(
        session=session,
        company=company,
        changed_by="t",
        partner_id=partner.id,
        issue_date=day,
        service_date=day,
        currency_code=currency,
        exchange_rate=rate,
        lines=[
            {
                "description": "Leistung",
                "quantity": "1",
                "unit_price": net,
                "tax_code_id": tax_code.id,
                "revenue_account_id": accounts["8400"].id,
            },
        ],
    )
    return issue_sales_invoice(
        session=session, invoice_id=draft.id, changed_by="t", upload_dir=upload_dir
    )


def _pay(session, item_id, foreign, day, bank_rate="1.22"):
    return settle_open_item(
        session=session,
        open_item_id=item_id,
        changed_by="t",
        foreign_amount=Decimal(foreign),
        exchange_rate=Decimal(bank_rate),
        settlement_date=day,
    )


def _received(session, company, accounts, amount, day, tag):
    """Gebuchter Geldeingang in Euro auf dem Sammelkonto: Bank an Forderungen."""
    transaction = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=accounts["1200"].id,
        booking_date=day,
        amount=Decimal(amount),
        currency_code="EUR",
        purpose="Zahlung",
        status="open",
        dedup_hash=tag,
    )
    session.add(transaction)
    session.commit()
    return book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )


def _kennzahlen(session, company, period):
    start, end, _ = period_bounds(period)
    result = compute_vat_return_details(
        session=session, company_id=company.id, date_from=start, date_to=end
    )
    return {row.kennziffer: row.amount for row in result.rows}, result


def _tax(session, company, period):
    return _kennzahlen(session, company, period)[0]["USt"]


def _balance(session, account) -> Decimal:
    """Saldo Soll minus Haben."""
    rows = session.execute(
        select(JournalEntryLine.debit_amount, JournalEntryLine.credit_amount).where(
            JournalEntryLine.account_id == account.id
        )
    ).all()
    return sum((debit - credit for debit, credit in rows), ZERO)


def _snapshot(session, company, period) -> dict[str, str]:
    row = session.execute(
        select(VatReturn).where(
            VatReturn.company_id == company.id, VatReturn.period_label == period
        )
    ).scalar_one()
    return {item["kennziffer"]: item["amount"] for item in row.kennzahlen}


def _save(session, company, period):
    return save_vat_return(
        session=session, company_id=company.id, period_label=period, changed_by="t"
    )


# ---------------------------------------------------------------------------
# Teilzahlungen über mehrere Monate
# ---------------------------------------------------------------------------


def test_partial_payments_count_at_the_rate_of_their_month(
    session: Session, tmp_path: Path
) -> None:
    """50 %, 20 % und 30 % im Februar, März und Mai, je zum Kurs dieses Monats.

    Februar: 95,00 USD / 1,20 = 79,17 € Steuer, 500,00 USD / 1,20 = 416,67 € Entgelt.
    März: 38,00 USD / 1,30 = 29,23 €, 200,00 USD / 1,30 = 153,85 €.
    Mai: 57,00 USD / 1,10 = 51,82 €, 300,00 USD / 1,10 = 272,73 €.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    assert _balance(session, accounts["1776"]) == Decimal("-152.00")

    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))
    _pay(session, issued.open_item_id, "238.00", date(2027, 3, 15))
    _pay(session, issued.open_item_id, "357.00", date(2027, 5, 20))

    expected = {
        "2027-01": (Decimal("0"), ZERO),
        "2027-02": (Decimal("416"), Decimal("79.17")),
        "2027-03": (Decimal("153"), Decimal("29.23")),
        "2027-04": (Decimal("0"), ZERO),
        "2027-05": (Decimal("272"), Decimal("51.82")),
    }
    for period, (base, tax) in expected.items():
        kennzahlen, result = _kennzahlen(session, company, period)
        assert kennzahlen["81"] == base, period
        assert kennzahlen["USt"] == tax, period
        assert kennzahlen["83"] == tax, period
        assert not any("bitte die Steuer prüfen" in warning for warning in result.warnings)

    # Ein Quartal ist die Summe seiner Monate, jeder Monat zu seinem Kurs.
    first_quarter = _kennzahlen(session, company, "2027-Q1")[0]
    assert first_quarter["USt"] == Decimal("108.40")
    assert first_quarter["81"] == Decimal("570")  # 416,67 + 153,85 = 570,52

    # Gebucht sind 152,00 €, je Zahlung anteilig 76,00, 30,40 und 45,60.
    february = _kennzahlen(session, company, "2027-02")[1].foreign_payments
    assert [(event.tax, event.booked_tax) for event in february] == [
        (Decimal("79.17"), Decimal("76.00"))
    ]
    assert february[0].rate == Decimal("1.200000")
    assert february[0].rate_month == date(2027, 2, 1)
    assert february[0].foreign_amount == Decimal("595.00")


def test_saving_each_month_brings_the_tax_account_to_the_reported_tax(
    session: Session, tmp_path: Path
) -> None:
    """Februar −3,17, März +1,17, Mai −6,22: 1776 steht am Ende auf 160,22 € statt 152,00 €."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))
    _pay(session, issued.open_item_id, "238.00", date(2027, 3, 15))
    _pay(session, issued.open_item_id, "357.00", date(2027, 5, 20))

    saved_february = _save(session, company, "2027-02")
    entry = session.get(JournalEntry, saved_february.fx_journal_entry_id)
    assert entry.source == SOURCE_VAT_FX_DIFFERENCE
    assert entry.entry_date == date(2027, 2, 28)
    assert {
        line.account_id: (line.debit_amount, line.credit_amount) for line in entry.lines
    } == {
        accounts["1776"].id: (ZERO, Decimal("3.17")),
        accounts["2150"].id: (Decimal("3.17"), ZERO),
    }

    saved_march = _save(session, company, "2027-03")
    assert session.get(JournalEntry, saved_march.fx_journal_entry_id).entry_date == date(
        2027, 3, 31
    )
    assert _balance(session, accounts["2660"]) == Decimal("-1.17")
    assert _balance(session, accounts["1776"]) == Decimal("-154.00")  # 152,00 + 3,17 − 1,17

    # Ein Monat ohne Zahlung bucht nichts.
    assert _save(session, company, "2027-04").fx_journal_entry_id is None
    _save(session, company, "2027-05")

    reported = sum(
        Decimal(_snapshot(session, company, period)["USt"])
        for period in ("2027-02", "2027-03", "2027-05")
    )
    assert reported == Decimal("160.22")
    assert _balance(session, accounts["1776"]) == -reported
    assert _balance(session, accounts["2150"]) == Decimal("9.39")  # 3,17 + 6,22


def test_tax_on_a_fully_paid_invoice_splits_the_foreign_tax_without_remainder(
    session: Session, tmp_path: Path
) -> None:
    """Drei Drittel: 63,33 + 63,34 + 63,33 = 190,00 USD; bei Kurs 1,00 also genau 190,00 €."""
    company, accounts = _seed(session)
    _monthly_rates(
        session, company, {"2027-02": "1.00", "2027-03": "1.00", "2027-04": "1.00"}
    )
    issued = _invoice(session, company, accounts, tmp_path, rate="1.00")
    part = Decimal("396.67")
    _pay(session, issued.open_item_id, str(part), date(2027, 2, 10))
    _pay(session, issued.open_item_id, str(part), date(2027, 3, 10))
    _pay(session, issued.open_item_id, str(Decimal("1190.00") - 2 * part), date(2027, 4, 10))
    taxes = [_tax(session, company, month) for month in ("2027-02", "2027-03", "2027-04")]
    assert taxes == [Decimal("63.33"), Decimal("63.34"), Decimal("63.33")]
    assert sum(taxes) == Decimal("190.00")


def test_tax_uses_the_exact_foreign_tax_at_high_rates(session: Session, tmp_path: Path) -> None:
    """19.000 JPY / 176,91 = 107,40 €. Über den gerundeten Eurobetrag käme 107,39 € heraus."""
    company, accounts = _seed(session)
    _monthly_rates(session, company, {"2027-02": "176.91"}, currency="JPY")
    issued = _invoice(
        session, company, accounts, tmp_path, currency="JPY", rate="183.94", net="100000.00"
    )
    assert _balance(session, accounts["1776"]) == Decimal("-103.29")
    _pay(session, issued.open_item_id, "119000.00", date(2027, 2, 10), bank_rate="176.00")
    kennzahlen = _kennzahlen(session, company, "2027-02")[0]
    assert kennzahlen["USt"] == Decimal("107.40")
    assert kennzahlen["81"] == Decimal("565")  # 100.000 / 176,91 = 565,26


def test_item_on_a_plain_entry_derives_foreign_amounts_from_the_invoice_rate(
    session: Session,
) -> None:
    """Zeilen ohne Fremdbetrag: 152,00 € × 1,25 = 190,00 USD, also 79,17 € zur Hälfte im Februar."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 1, 15),
            description="Rechnung",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(account_id=accounts["1400"].id, debit_amount=Decimal("952.00")),
                JournalLineInput(
                    account_id=accounts["8400"].id,
                    credit_amount=Decimal("800.00"),
                    tax_code_id=tax_code.id,
                ),
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    credit_amount=Decimal("152.00"),
                    tax_code_id=tax_code.id,
                ),
            ],
        ),
    )
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=accounts["1400"].id,
            item_type="receivable",
            reference="RE-EINFACH",
            entry_date=date(2027, 1, 15),
            amount=Decimal("0"),
            changed_by="t",
            foreign_currency_code="USD",
            foreign_amount=Decimal("1190.00"),
            exchange_rate=Decimal("1.25"),
            journal_entry_id=entry.id,
        ),
    )
    _pay(session, item.id, "595.00", date(2027, 2, 10))
    assert _tax(session, company, "2027-02") == Decimal("79.17")


# ---------------------------------------------------------------------------
# Storno und Neubuchung
# ---------------------------------------------------------------------------


def test_reversal_takes_back_the_tax_at_the_rate_of_the_payment(
    session: Session, tmp_path: Path
) -> None:
    """Februar +79,17 €; der Storno im April −79,17 €, nicht 95,00 / 1,10 = 86,36 €."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    received = _received(session, company, accounts, "495.00", date(2027, 2, 10), "storno")
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        bank_transaction_id=received.id,
        foreign_amount=Decimal("595.00"),
        amount=Decimal("495.00"),
    )
    saved = _save(session, company, "2027-02")
    assert saved.fx_journal_entry_id is not None
    assert _balance(session, accounts["1776"]) == Decimal("-155.17")  # 152,00 + 3,17

    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    assert _tax(session, company, "2027-02") == Decimal("79.17")
    assert _tax(session, company, "2027-03") == ZERO
    april = _kennzahlen(session, company, "2027-04")
    assert april[0]["USt"] == Decimal("-79.17")
    assert april[0]["81"] == Decimal("-416")  # abgerundet wie jede Bemessungsgrundlage
    assert [(event.is_reversal, event.tax) for event in april[1].foreign_payments] == [
        (True, Decimal("-79.17"))
    ]

    # Das Steuerkonto kommt mit der Meldung des Stornos auf den gebuchten Betrag zurück.
    _save(session, company, "2027-04")
    assert _balance(session, accounts["1776"]) == Decimal("-152.00")
    assert _balance(session, accounts["2150"]) == Decimal("3.17") + Decimal("0.00")
    assert _balance(session, accounts["2660"]) == Decimal("-3.17")


def test_rebooked_payment_keeps_the_rate_of_the_month_the_money_came_in(
    session: Session, tmp_path: Path
) -> None:
    """Storno im März und Neubuchung desselben Umsatzes: März −79,17 + 79,17 = 0.

    Zum Märzkurs stünde dort 73,08 − 79,17 = −6,09 €, und der Februar wäre rückwirkend falsch.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    received = _received(session, company, accounts, "495.00", date(2027, 2, 10), "neu")
    terms = {
        "open_item_id": issued.open_item_id,
        "changed_by": "t",
        "bank_transaction_id": received.id,
        "foreign_amount": Decimal("595.00"),
        "amount": Decimal("495.00"),
    }
    settle_open_item(session=session, **terms)
    _save(session, company, "2027-02")

    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=received.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(session=session, **terms)

    settlements = (
        session.execute(select(OpenItemSettlement).order_by(OpenItemSettlement.id)).scalars().all()
    )
    assert settlements[1].settlement_date == date(2027, 3, 5)
    assert settlements[1].received_on == date(2027, 2, 10)

    assert _tax(session, company, "2027-02") == Decimal("79.17")
    march = _kennzahlen(session, company, "2027-03")
    assert march[0]["USt"] == ZERO
    assert march[0]["81"] == Decimal("0")
    assert sorted(event.tax for event in march[1].foreign_payments) == [
        Decimal("-79.17"),
        Decimal("79.17"),
    ]

    # Zahlung, Storno und Neubuchung ändern das Steuerkonto zusammen nur um die Februar-Differenz.
    saved = _save(session, company, "2027-03")
    assert saved.fx_journal_entry_id is None
    assert _balance(session, accounts["1776"]) == Decimal("-155.17")


def test_payment_moved_to_another_invoice_keeps_its_month(
    session: Session, tmp_path: Path
) -> None:
    """Falsch zugeordnet und korrigiert: April −79,17 auf der einen, +79,17 auf der anderen."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    wrong = _invoice(session, company, accounts, tmp_path)
    right = _invoice(session, company, accounts, tmp_path, day=date(2027, 1, 16))
    received = _received(session, company, accounts, "495.00", date(2027, 2, 10), "falsch")
    terms = {
        "changed_by": "t",
        "bank_transaction_id": received.id,
        "foreign_amount": Decimal("595.00"),
        "amount": Decimal("495.00"),
    }
    settle_open_item(session=session, open_item_id=wrong.open_item_id, **terms)
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=received.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(session=session, open_item_id=right.open_item_id, **terms)
    assert _tax(session, company, "2027-02") == Decimal("79.17")
    assert _tax(session, company, "2027-04") == ZERO


def test_reversal_and_repayment_of_a_part_keep_the_foreign_tax_exact(
    session: Session, tmp_path: Path
) -> None:
    """Zwei Drittel gezahlt, das erste storniert und neu gezahlt: die Teile bleiben gleich groß."""
    company, accounts = _seed(session)
    _monthly_rates(session, company, {"2027-02": "1.00", "2027-03": "1.00", "2027-04": "1.00"})
    issued = _invoice(session, company, accounts, tmp_path, rate="1.00")
    first = _received(session, company, accounts, "396.67", date(2027, 2, 10), "a")
    second = _received(session, company, accounts, "396.67", date(2027, 3, 10), "b")
    for transaction in (first, second):
        settle_open_item(
            session=session,
            open_item_id=issued.open_item_id,
            changed_by="t",
            bank_transaction_id=transaction.id,
            foreign_amount=Decimal("396.67"),
            amount=Decimal("396.67"),
        )
    february, march = _tax(session, company, "2027-02"), _tax(session, company, "2027-03")
    assert (february, march) == (Decimal("63.33"), Decimal("63.34"))
    reverse_journal_entry(
        session=session,
        journal_entry_id=first.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=first.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        bank_transaction_id=first.id,
        foreign_amount=Decimal("396.67"),
        amount=Decimal("396.67"),
    )
    assert _tax(session, company, "2027-02") == february
    assert _tax(session, company, "2027-03") == march
    assert _tax(session, company, "2027-04") == ZERO


def test_cancelled_unpaid_invoice_owes_nothing_and_books_nothing(
    session: Session, tmp_path: Path
) -> None:
    from app.services.sales_invoices import cancel_sales_invoice

    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    cancel_sales_invoice(
        session=session,
        invoice_id=issued.id,
        changed_by="t",
        cancellation_date=date(2027, 2, 10),
        reason="Falsch",
        upload_dir=tmp_path,
    )
    assert _tax(session, company, "2027-01") == ZERO
    assert _tax(session, company, "2027-02") == ZERO
    assert _save(session, company, "2027-02").fx_journal_entry_id is None
    assert _balance(session, accounts["1776"]) == ZERO


# ---------------------------------------------------------------------------
# Hauptbuch
# ---------------------------------------------------------------------------


def test_books_show_revenue_tax_and_fx_result_on_the_entgelt(
    session: Session, tmp_path: Path
) -> None:
    """991,67 € Geldeingang = 800,00 € Erlös + 158,33 € Umsatzsteuer + 33,34 € Kursgewinn.

    Die Zahlung bucht 39,67 € Kursgewinn auf das Brutto, die Steuerdifferenz nimmt 6,33 € davon.
    Das Entgelt allein ergibt 1.000,00 / 1,20 − 800,00 = 33,33 €; der Cent ist Rundung.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    received = _received(session, company, accounts, "991.67", date(2027, 2, 10), "voll")
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        bank_transaction_id=received.id,
        foreign_amount=Decimal("1190.00"),
        amount=Decimal("991.67"),
    )
    assert _balance(session, accounts["2660"]) == Decimal("-39.67")
    _save(session, company, "2027-02")

    assert _balance(session, accounts["1200"]) == Decimal("991.67")
    assert _balance(session, accounts["1400"]) == ZERO
    assert _balance(session, accounts["8400"]) == Decimal("-800.00")
    assert _balance(session, accounts["1776"]) == Decimal("-158.33")
    fx_result = -(_balance(session, accounts["2660"]) + _balance(session, accounts["2150"]))
    assert fx_result == Decimal("33.34")
    assert Decimal(_snapshot(session, company, "2027-02")["USt"]) == Decimal("158.33")


def test_difference_entry_is_not_revenue_and_cannot_be_changed(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))
    before = _kennzahlen(session, company, "2027-02")[0]
    saved = _save(session, company, "2027-02")
    assert saved.fx_journal_entry_id is not None
    assert _kennzahlen(session, company, "2027-02")[0] == before

    with pytest.raises(JournalEntryCreationError, match="gemeldete Umsatzsteuer"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=saved.fx_journal_entry_id,
            reversal_date=date(2027, 3, 1),
            changed_by="t",
        )
    with pytest.raises(JournalEntryCreationError, match="gemeldete Umsatzsteuer"):
        amend_journal_entry(
            session=session,
            company_id=company.id,
            journal_entry_id=saved.fx_journal_entry_id,
            changed_by="t",
            partner_id=None,
        )


def test_difference_moves_out_of_a_locked_month(session: Session, tmp_path: Path) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))
    february = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 2, 1),
            description="Monat anlegen",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal("1.00")),
                JournalLineInput(account_id=accounts["8400"].id, credit_amount=Decimal("1.00")),
            ],
        ),
    )
    lock_period(session=session, period_id=february.period_id, locked_by="t")
    saved = _save(session, company, "2027-02")
    entry = session.get(JournalEntry, saved.fx_journal_entry_id)
    assert entry.entry_date == date(2027, 3, 1)
    assert _balance(session, accounts["1776"]) == Decimal("-155.17")


def test_missing_fx_account_stops_saving_without_a_trace(session: Session, tmp_path: Path) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))
    session.delete(accounts["2150"])
    session.commit()
    with pytest.raises(VatReturnError, match="Steuerdifferenz"):
        _save(session, company, "2027-02")
    assert session.execute(select(VatReturn)).scalars().all() == []
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.vat_rate is None
    assert settlement.vat_fx_entry_id is None
    assert _balance(session, accounts["1776"]) == Decimal("-152.00")


def test_annual_return_adds_up_the_months_and_books_only_what_is_left(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))
    _pay(session, issued.open_item_id, "238.00", date(2027, 3, 15))
    _pay(session, issued.open_item_id, "357.00", date(2027, 5, 20))
    _save(session, company, "2027-02")
    entries_before = session.execute(
        select(JournalEntry).where(JournalEntry.source == SOURCE_VAT_FX_DIFFERENCE)
    ).scalars().all()
    assert len(entries_before) == 1

    annual = _save(session, company, "2027")
    assert Decimal(_snapshot(session, company, "2027")["USt"]) == Decimal("160.22")
    # Februar ist gebucht; Rest ist März +1,17 und Mai −6,22.
    entry = session.get(JournalEntry, annual.fx_journal_entry_id)
    assert entry.entry_date == date(2027, 12, 31)
    assert _balance(session, accounts["1776"]) == Decimal("-160.22")

    # Spätere Monatsmeldungen buchen nichts mehr und ändern die Kennzahlen nicht.
    assert _save(session, company, "2027-03").fx_journal_entry_id is None
    assert _snapshot(session, company, "2027-03")["USt"] == "29.23"
    assert _balance(session, accounts["1776"]) == Decimal("-160.22")


# ---------------------------------------------------------------------------
# Gemeldete Zeiträume und fehlende Kurse
# ---------------------------------------------------------------------------


def test_reported_period_keeps_its_rate_when_rates_change_later(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    received = _received(session, company, accounts, "495.00", date(2027, 2, 10), "kurs")
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        bank_transaction_id=received.id,
        foreign_amount=Decimal("595.00"),
        amount=Decimal("495.00"),
    )
    _save(session, company, "2027-02")
    snapshot = _snapshot(session, company, "2027-02")
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.vat_rate == Decimal("1.200000")
    balance = _balance(session, accounts["1776"])

    _monthly_rates(session, company, {"2027-02": "1.50"})
    assert _tax(session, company, "2027-02") == Decimal("79.17")
    assert _snapshot(session, company, "2027-02") == snapshot
    assert _balance(session, accounts["1776"]) == balance

    # Auch der spätere Storno nimmt den gemeldeten Betrag zurück.
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    assert _tax(session, company, "2027-04") == Decimal("-79.17")


def test_saving_needs_the_monthly_rate_and_the_preview_says_so(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "595.00", date(2027, 2, 10))

    # Vorschau zum Kurs der Rechnung: 95,00 USD / 1,25 = 76,00 €.
    kennzahlen, result = _kennzahlen(session, company, "2027-02")
    assert kennzahlen["USt"] == Decimal("76.00")
    assert any("USD 2027-02" in warning for warning in result.warnings)
    assert result.foreign_payments[0].rate_missing is True

    with pytest.raises(VatReturnError, match="USD 2027-02"):
        _save(session, company, "2027-02")
    assert session.execute(select(VatReturn)).scalars().all() == []

    _monthly_rates(session, company)
    kennzahlen, result = _kennzahlen(session, company, "2027-02")
    assert kennzahlen["USt"] == Decimal("79.17")
    assert not any("USD 2027-02" in warning for warning in result.warnings)
    assert any("3,17 €" in warning for warning in result.warnings)
    assert _save(session, company, "2027-02").period_label == "2027-02"
    # Danach ist nichts mehr offen, der Hinweis auf die Differenz entfällt.
    warnings = _kennzahlen(session, company, "2027-02")[1].warnings
    assert not any("Beim Festhalten" in warning for warning in warnings)


def test_mixed_currencies_on_one_entry_stay_at_book_value_with_a_warning(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    second = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=accounts["1400"].id,
            item_type="receivable",
            reference="RE-CHF",
            entry_date=date(2027, 1, 15),
            amount=Decimal("0"),
            changed_by="t",
            foreign_currency_code="CHF",
            foreign_amount=Decimal("100.00"),
            exchange_rate=Decimal("1.00"),
            journal_entry_id=issued.journal_entry_id,
        ),
    )
    _pay(session, second.id, "100.00", date(2027, 2, 10), bank_rate="1.00")
    kennzahlen, result = _kennzahlen(session, company, "2027-02")
    assert kennzahlen["USt"] > ZERO
    assert any("verschiedenen Währungen" in warning for warning in result.warnings)
    assert result.foreign_payments == []


def test_accrual_method_and_daily_rates_ignore_monthly_rates(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session, cash_basis=False)
    _monthly_rates(session, company, {"2027-01": "2.00"})
    set_exchange_rate(
        session=session,
        company=company,
        currency_code="USD",
        rate_date=date(2026, 12, 30),
        rate="1.11",
        changed_by="t",
    )
    # Der Monatskurs ist kein Tageskurs, auch nicht am Monatsersten.
    found = find_exchange_rate(
        session=session, tenant_id=company.tenant_id, currency_code="USD", on=date(2027, 1, 2)
    )
    assert found is not None
    assert found.rate == Decimal("1.110000")

    issued = _invoice(session, company, accounts, tmp_path)
    assert issued.exchange_rate == Decimal("1.250000")
    assert _tax(session, company, "2027-01") == Decimal("152.00")
    assert _save(session, company, "2027-01").fx_journal_entry_id is None


# ---------------------------------------------------------------------------
# Zufallsläufe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_random_payments_reversals_and_rebookings_keep_the_invariants(
    session: Session, tmp_path: Path, seed: int
) -> None:
    """Geld, Steuerkonto und Meldungen über zufällige Abläufe, Monat für Monat festgehalten."""
    rng = random.Random(seed)
    company, accounts = _seed(session)
    rates = {
        f"2027-{month:02d}": str(Decimal(rng.randint(90, 160)) / Decimal(100))
        for month in range(1, 13)
    }
    _monthly_rates(session, company, rates)
    net = Decimal(rng.randint(10_000, 500_000)) / Decimal(100)
    invoice_rate = Decimal(rng.randint(90, 160)) / Decimal(100)
    issued = _invoice(
        session,
        company,
        accounts,
        tmp_path,
        rate=str(invoice_rate),
        net=str(net),
        day=date(2027, 1, 5),
    )
    tax_foreign = (net * Decimal("0.19")).quantize(ZERO, rounding=ROUND_HALF_UP)
    gross = net + tax_foreign
    booked_tax = -_balance(session, accounts["1776"])
    item_id = issued.open_item_id

    reported: dict[str, Decimal] = {}
    open_foreign = gross
    active: list[dict] = []
    tag = 0
    for month in range(2, 13):
        period = f"2027-{month:02d}"
        for _ in range(rng.randint(0, 2)):
            action = rng.random()
            day = date(2027, month, rng.randint(1, 28))
            if action < 0.55 and open_foreign > Decimal("0.05"):
                tag += 1
                foreign = (
                    open_foreign
                    if rng.random() < 0.25
                    else (open_foreign * Decimal(rng.randint(5, 90)) / Decimal(100)).quantize(ZERO)
                )
                if foreign <= Decimal("0.05"):
                    continue
                euro = (foreign / Decimal(rates[period])).quantize(ZERO, rounding=ROUND_HALF_UP)
                transaction = _received(session, company, accounts, str(euro), day, f"z{tag}")
                terms = {
                    "bank_transaction_id": transaction.id,
                    "foreign_amount": foreign,
                    "amount": euro,
                }
                settle_open_item(
                    session=session, open_item_id=item_id, changed_by="t", **terms
                )
                active.append({"transaction": transaction, "terms": terms})
                open_foreign -= foreign
            elif action < 0.8 and active:
                victim = rng.choice(active)
                transaction = victim["transaction"]
                reverse_journal_entry(
                    session=session,
                    journal_entry_id=transaction.journal_entry_id,
                    reversal_date=max(day, transaction.booking_date),
                    changed_by="t",
                )
                open_foreign += victim["terms"]["foreign_amount"]
                active.remove(victim)
                if rng.random() < 0.6:
                    book_transaction(
                        session=session,
                        transaction_id=transaction.id,
                        contra_account_id=accounts["1400"].id,
                        changed_by="t",
                    )
                    settle_open_item(
                        session=session,
                        open_item_id=item_id,
                        changed_by="t",
                        **victim["terms"],
                    )
                    open_foreign -= victim["terms"]["foreign_amount"]
                    active.append(victim)
        _save(session, company, period)
        reported[period] = Decimal(_snapshot(session, company, period)["USt"])

    # Gemeldete Zeiträume ändern sich durch alles Spätere nicht.
    for period, tax in reported.items():
        assert _tax(session, company, period) == tax, period

    # Jede aktive Zahlung zählt mit ihrem Monatskurs, jede stornierte hebt sich auf.
    settlements = session.execute(
        select(OpenItemSettlement).where(OpenItemSettlement.open_item_id == item_id)
    ).scalars().all()
    paid_foreign = sum(
        (row.foreign_amount for row in settlements if row.reversed_on is None), ZERO
    )
    assert paid_foreign == gross - open_foreign
    total_reported = sum(reported.values(), ZERO)
    expected_about = sum(
        (
            row.foreign_amount * tax_foreign / gross / row.vat_rate
            for row in settlements
            if row.reversed_on is None
        ),
        Decimal(0),
    )
    assert abs(total_reported - expected_about) <= Decimal("0.02") * max(len(settlements), 1)

    # Gezahlt und gemeldet passen je Vorgang: Fremdbetrag, Kurs und Steuer sind konsistent.
    for period in reported:
        for event in _kennzahlen(session, company, period)[1].foreign_payments:
            assert event.rate == event.settlement.vat_rate
            about = event.foreign_amount * tax_foreign / gross / event.rate
            assert abs(event.tax - about) <= Decimal("0.02"), period

    # Das Steuerkonto zeigt die gemeldete Steuer plus den noch nicht fälligen Teil.
    not_due = booked_tax * open_foreign / gross
    assert abs(-_balance(session, accounts["1776"]) - total_reported - not_due) <= Decimal(
        "0.02"
    ) * max(len(settlements), 1)
    # Alles gemeldet und gebucht: Nichts bleibt für eine weitere Meldung übrig.
    annual = _save(session, company, "2027")
    if not any(row.settlement_date.month == 1 for row in settlements):
        assert annual.fx_journal_entry_id is None
    assert Decimal(_snapshot(session, company, "2027")["USt"]) == total_reported


# ---------------------------------------------------------------------------
# Funde des Reviews: Kursmonat, gemeldete Zeiträume, Grenzen
# ---------------------------------------------------------------------------


def _settled_full(session, company, accounts, tmp_path, tag="voll", day=date(2027, 2, 10)):
    """Die ganze Rechnung über einen gebuchten Bankumsatz bezahlt; im Februar 158,33 € Steuer."""
    issued = _invoice(session, company, accounts, tmp_path)
    received = _received(session, company, accounts, "991.67", day, tag)
    terms = {
        "open_item_id": issued.open_item_id,
        "changed_by": "t",
        "bank_transaction_id": received.id,
        "foreign_amount": Decimal("1190.00"),
        "amount": Decimal("991.67"),
    }
    settle_open_item(session=session, **terms)
    return issued, received, terms


def _manual_receipt(session, company, accounts, day, amount="991.67"):
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description="Zahlung",
            status="posted",
            changed_by="t",
            lines=[
                JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal(amount)),
                JournalLineInput(account_id=accounts["1400"].id, credit_amount=Decimal(amount)),
            ],
        ),
    )


def test_rate_follows_the_bank_date_when_a_locked_month_forces_a_new_booking(
    session: Session, tmp_path: Path
) -> None:
    """Der Bankumsatz vom 10.2. bleibt Februar, auch über eine neue Buchung vom 6.4.

    Zum Aprilkurs stünde im April −158,33 + 172,73 = 14,40 €, und 1776 ginge auf −172,73 €.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _, received, terms = _settled_full(session, company, accounts, tmp_path)
    february = session.get(JournalEntry, received.journal_entry_id)
    _save(session, company, "2027-02")
    lock_period(session=session, period_id=february.period_id, locked_by="t")
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    replacement = _manual_receipt(session, company, accounts, date(2027, 4, 6))
    settle_open_item(session=session, journal_entry_id=replacement.id, **terms)

    settlement = session.execute(
        select(OpenItemSettlement).order_by(OpenItemSettlement.id.desc())
    ).scalars().first()
    assert settlement.settlement_date == date(2027, 4, 6)
    assert settlement.received_on == date(2027, 2, 10)
    assert _tax(session, company, "2027-04") == ZERO
    _save(session, company, "2027-04")
    assert _balance(session, accounts["1776"]) == Decimal("-158.33")


@pytest.mark.parametrize(
    ("explicit", "booking"),
    [(date(2027, 3, 5), None), (None, date(2027, 3, 1))],
    ids=["genanntes Datum", "Buchung im Folgemonat"],
)
def test_rate_month_is_the_bank_date_whatever_the_settlement_date_says(
    session: Session, tmp_path: Path, explicit, booking
) -> None:
    """Geld am 27.2.: 190,00 USD / 1,20 = 158,33 €. Zum Märzkurs wären es 146,15 €."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    received = _received(session, company, accounts, "991.67", date(2027, 2, 27), "datum")
    entry = None
    if booking is not None:
        reverse_journal_entry(
            session=session,
            journal_entry_id=received.journal_entry_id,
            reversal_date=date(2027, 2, 27),
            changed_by="t",
        )
        entry = _manual_receipt(session, company, accounts, booking)
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="t",
        bank_transaction_id=received.id,
        journal_entry_id=entry.id if entry is not None else None,
        settlement_date=explicit,
        foreign_amount=Decimal("1190.00"),
        amount=Decimal("991.67"),
    )
    assert _tax(session, company, "2027-03") == Decimal("158.33")
    assert _tax(session, company, "2027-02") == ZERO


def test_rate_month_without_a_bank_transaction_is_the_given_date(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    _pay(session, issued.open_item_id, "1190.00", date(2027, 3, 5))
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.received_on == date(2027, 3, 5)
    assert _tax(session, company, "2027-03") == Decimal("146.15")  # 190,00 / 1,30


def test_reassignment_after_a_reported_reversal_goes_behind_the_reported_month(
    session: Session, tmp_path: Path
) -> None:
    """Februar +158,33, März −158,33 (gemeldet), die Neuzuordnung zählt im April mit Februarkurs.

    Auf dem Stornotag läge sie im gemeldeten März: Dessen Neuberechnung wäre 0,00 statt −158,33, und
    die Meldungen ergäben in Summe 0,00, obwohl das Geld da ist.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _, received, terms = _settled_full(session, company, accounts, tmp_path)
    _save(session, company, "2027-02")
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    _save(session, company, "2027-03")
    assert _snapshot(session, company, "2027-03")["USt"] == "-158.33"

    book_transaction(
        session=session,
        transaction_id=received.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(session=session, **terms)
    settlement = session.execute(
        select(OpenItemSettlement).order_by(OpenItemSettlement.id.desc())
    ).scalars().first()
    # Der echte Tag bleibt, für die UStVA zählt der erste offene.
    assert settlement.settlement_date == date(2027, 3, 5)
    assert settlement.vat_settled_on == date(2027, 4, 1)
    assert settlement.received_on == date(2027, 2, 10)

    assert _tax(session, company, "2027-02") == Decimal("158.33")
    assert _tax(session, company, "2027-03") == Decimal("-158.33")
    assert _tax(session, company, "2027-04") == Decimal("158.33")
    _save(session, company, "2027-04")
    assert _balance(session, accounts["1776"]) == Decimal("-158.33")


def test_reversal_dated_in_a_reported_month_counts_in_the_next_open_one(
    session: Session, tmp_path: Path
) -> None:
    """Storno mit Datum 20.2. nach der Februar-Meldung: Februar bleibt, der März trägt −158,33 €."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _, received, _ = _settled_full(session, company, accounts, tmp_path)
    _save(session, company, "2027-02")
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 2, 20),
        changed_by="t",
    )
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.reversed_on == date(2027, 2, 20)
    assert settlement.vat_reversed_on == date(2027, 3, 1)
    assert _tax(session, company, "2027-02") == Decimal("158.33")
    assert _tax(session, company, "2027-03") == Decimal("-158.33")
    _save(session, company, "2027-03")
    assert _balance(session, accounts["1776"]) == Decimal("-152.00")


def _late_payment(session, company, accounts, tmp_path):
    """Januar ist mit 0,00 festgehalten, danach wird ein Bankumsatz vom 20.1. zugeordnet."""
    _monthly_rates(session, company, {"2027-01": "1.20"})
    issued = _invoice(session, company, accounts, tmp_path, day=date(2027, 1, 5))
    _save(session, company, "2027-01")
    received = _received(session, company, accounts, "991.67", date(2027, 1, 20), "spaet")
    terms = {
        "open_item_id": issued.open_item_id,
        "changed_by": "t",
        "bank_transaction_id": received.id,
        "foreign_amount": Decimal("1190.00"),
        "amount": Decimal("991.67"),
    }
    settle_open_item(session=session, **terms)
    return issued, received, terms


def _is_named_as_late(session, company, period) -> bool:
    warnings = _kennzahlen(session, company, period)[1].warnings
    return any("erst danach zugeordnet" in warning for warning in warnings)


@pytest.mark.parametrize(
    "reversal_day",
    [date(2027, 1, 25), date(2027, 2, 15)],
    ids=["im gemeldeten Januar", "im offenen Februar"],
)
def test_reversal_of_a_never_reported_payment_reports_nothing(
    session: Session, tmp_path: Path, reversal_day
) -> None:
    """Nie gemeldet, also nichts zu mindern: Es bleibt bei 0,00 €, nicht −158,33 € im Februar."""
    company, accounts = _seed(session)
    _, received, _ = _late_payment(session, company, accounts, tmp_path)
    assert _tax(session, company, "2027-01") == Decimal("158.33")
    assert _is_named_as_late(session, company, "2027-01")

    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=reversal_day,
        changed_by="t",
    )
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.reversed_on == reversal_day
    assert settlement.vat_reversed_on == date(2027, 1, 20)
    reported = Decimal(_snapshot(session, company, "2027-01")["USt"])
    assert _tax(session, company, "2027-01") == reported
    assert _tax(session, company, "2027-02") == ZERO
    assert not _is_named_as_late(session, company, "2027-01")
    assert _save(session, company, "2027-02").fx_journal_entry_id is None
    assert _balance(session, accounts["1776"]) == Decimal("-152.00")


def test_reassigning_a_never_reported_payment_reports_it_in_the_next_open_month(
    session: Session, tmp_path: Path
) -> None:
    """Der naheliegende Weg nach „bitte berichtigen“: Storno, Neuzuordnung, Februar 158,33 €."""
    company, accounts = _seed(session)
    _, received, terms = _late_payment(session, company, accounts, tmp_path)
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 1, 25),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=received.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(session=session, **terms)
    settlement = session.execute(
        select(OpenItemSettlement).order_by(OpenItemSettlement.id.desc())
    ).scalars().first()
    assert settlement.settlement_date == date(2027, 1, 25)
    assert settlement.vat_settled_on == date(2027, 2, 1)
    assert settlement.received_on == date(2027, 1, 20)

    assert _tax(session, company, "2027-01") == ZERO
    assert _tax(session, company, "2027-02") == Decimal("158.33")
    assert not _is_named_as_late(session, company, "2027-02")
    _save(session, company, "2027-02")
    assert _balance(session, accounts["1776"]) == Decimal("-158.33")


def test_valuation_reads_the_real_day_of_a_reversal_after_the_return(
    session: Session, tmp_path: Path
) -> None:
    """Rücklastschrift 20.12. nach der Dezember-Meldung: Am 31.12. sind 952,00 € offen.

    1.190 USD / 1,50 = 793,33 €, Verlust 158,67 €. Mit dem Tag der UStVA fehlte die Zeile.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company, {"2027-12": "1.20"})
    set_exchange_rate(
        session=session,
        company=company,
        currency_code="USD",
        rate_date=date(2027, 12, 31),
        rate="1.50",
        changed_by="t",
    )
    _, received, _ = _settled_full(session, company, accounts, tmp_path, day=date(2027, 12, 10))
    _save(session, company, "2027-12")
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 12, 20),
        changed_by="t",
    )
    rows = fx_valuation_rows(session=session, company=company, as_of=date(2027, 12, 31))
    assert [(row.carrying, row.value, row.difference) for row in rows] == [
        (Decimal("952.00"), Decimal("793.33"), Decimal("-158.67"))
    ]
    assert _balance(session, accounts["1400"]) == Decimal("952.00")
    assert _tax(session, company, "2027-12") == Decimal("158.33")


def test_valuation_reads_the_real_day_of_a_reassignment_after_the_return(
    session: Session, tmp_path: Path
) -> None:
    """Am 31.12. ist nichts offen. Mit dem Tag der UStVA (1.1.) stünde dort ein falscher Ertrag."""
    company, accounts = _seed(session)
    _monthly_rates(session, company, {"2027-12": "1.20"})
    set_exchange_rate(
        session=session,
        company=company,
        currency_code="USD",
        rate_date=date(2027, 12, 31),
        rate="1.00",
        changed_by="t",
    )
    _, received, terms = _settled_full(session, company, accounts, tmp_path, day=date(2027, 12, 10))
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 12, 12),
        changed_by="t",
    )
    _save(session, company, "2027-12")
    book_transaction(
        session=session,
        transaction_id=received.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(session=session, **terms)
    assert fx_valuation_rows(session=session, company=company, as_of=date(2027, 12, 31)) == []
    assert _balance(session, accounts["1400"]) == ZERO
    assert _tax(session, company, "2027-12") == ZERO
    assert _tax(session, company, "2028-01") == Decimal("158.33")


def test_reported_period_shift_skips_quarters_and_only_touches_the_cash_basis(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _, received, _ = _settled_full(session, company, accounts, tmp_path)
    _save(session, company, "2027-Q1")
    _save(session, company, "2027-04")
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 2, 20),
        changed_by="t",
    )
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.reversed_on == date(2027, 2, 20)
    assert settlement.vat_reversed_on == date(2027, 5, 1)


def _direct_invoice(session, company, accounts, *, day=date(2027, 1, 15), with_tax=True):
    """Rechnung als Direktbuchung mit Posten, ohne Ausgangsrechnung."""
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    lines = [
        JournalLineInput(account_id=accounts["1400"].id, debit_amount=Decimal("952.00")),
        JournalLineInput(
            account_id=accounts["8400"].id,
            credit_amount=Decimal("800.00") if with_tax else Decimal("952.00"),
            tax_code_id=tax_code.id if with_tax else None,
        ),
    ]
    if with_tax:
        lines.append(
            JournalLineInput(
                account_id=tax_code.vat_account_id,
                credit_amount=Decimal("152.00"),
                tax_code_id=tax_code.id,
            )
        )
    invoice = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description="Rechnung",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=lines,
        ),
    )
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=accounts["1400"].id,
            item_type="receivable",
            reference=f"RE-DIREKT-{invoice.id}",
            entry_date=day,
            amount=Decimal("0"),
            changed_by="t",
            foreign_currency_code="USD",
            foreign_amount=Decimal("1190.00"),
            exchange_rate=Decimal("1.25"),
            journal_entry_id=invoice.id,
        ),
    )
    return invoice, item


def test_paid_direct_invoice_cannot_be_reversed_until_the_payment_is(session: Session) -> None:
    """Sonst bliebe der Rest der Differenz (6,33 €) dauerhaft auf dem Steuerkonto."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    invoice, item = _direct_invoice(session, company, accounts)
    received = _received(session, company, accounts, "991.67", date(2027, 2, 10), "direkt")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=received.id,
        foreign_amount=Decimal("1190.00"),
        amount=Decimal("991.67"),
    )
    with pytest.raises(JournalEntryCreationError, match="zuerst die Zahlung stornieren"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=invoice.id,
            reversal_date=date(2027, 3, 1),
            changed_by="t",
        )
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 3, 1),
        changed_by="t",
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2027, 3, 2),
        changed_by="t",
    )
    assert _balance(session, accounts["1776"]) == ZERO


def test_settlement_without_a_booking_falls_with_the_invoice(session: Session) -> None:
    """Nichts im Hauptbuch, also keine Sackgasse: Februar +158,33 €, März −158,33 €, 1776 auf 0."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    invoice, item = _direct_invoice(session, company, accounts)
    _pay(session, item.id, "1190.00", date(2027, 2, 10), bank_rate="1.20")
    _save(session, company, "2027-02")
    assert _balance(session, accounts["1776"]) == Decimal("-158.33")

    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.reversed_on == date(2027, 3, 5)
    assert _tax(session, company, "2027-02") == Decimal("158.33")
    assert _tax(session, company, "2027-03") == Decimal("-158.33")
    _save(session, company, "2027-03")
    assert _balance(session, accounts["1776"]) == ZERO


def test_entry_that_is_invoice_and_payment_can_be_reversed(session: Session) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    invoice, item = _direct_invoice(session, company, accounts)
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        journal_entry_id=invoice.id,
        foreign_amount=Decimal("1190.00"),
        amount=Decimal("952.00"),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    assert _balance(session, accounts["1776"]) == ZERO


@pytest.mark.parametrize("case", ["vor Ist", "nach Ist", "ohne Steuer"])
def test_reversal_stays_free_when_the_invoice_does_not_count_on_cash_basis(
    session: Session, case: str
) -> None:
    company, accounts = _seed(session, cash_basis=False)
    _monthly_rates(session, company)
    set_company_vat_method(
        session=session,
        company=company,
        method="ist",
        valid_from=date(2027, 3, 1),
        changed_by="t",
    )
    if case == "nach Ist":
        set_company_vat_method(
            session=session,
            company=company,
            method="soll",
            valid_from=date(2027, 5, 1),
            changed_by="t",
        )
    day = {"vor Ist": date(2027, 2, 5), "nach Ist": date(2027, 6, 5)}.get(case, date(2027, 3, 5))
    invoice, item = _direct_invoice(
        session, company, accounts, day=day, with_tax=case != "ohne Steuer"
    )
    received = _received(session, company, accounts, "991.67", day, f"frei-{case}")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=received.id,
        foreign_amount=Decimal("1190.00"),
        amount=Decimal("991.67"),
    )
    reverse_journal_entry(
        session=session, journal_entry_id=invoice.id, reversal_date=day, changed_by="t"
    )
    assert session.execute(
        select(JournalEntry.id).where(JournalEntry.reversal_of_id == invoice.id)
    ).first()


def test_cancelled_invoice_closes_its_item(session: Session) -> None:
    """Keine Zahlung mehr darauf, keine Mahnung, und der Hauptbuchwert 0,00 passt zum Posten."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    invoice, item = _direct_invoice(session, company, accounts)
    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2027, 1, 20),
        changed_by="t",
    )
    session.refresh(item)
    assert (item.status, item.open_amount, item.foreign_open_amount) == (
        "settled",
        ZERO,
        ZERO,
    )
    with pytest.raises(OpenItemError, match="bereits ausgeglichen"):
        _pay(session, item.id, "1190.00", date(2027, 2, 10))
    assert _balance(session, accounts["1400"]) == ZERO


def test_invoice_reversed_after_its_payment_closes_the_item_too(session: Session) -> None:
    """Der Weg „erst Zahlung, dann Rechnung“ endet ohne offenen Posten."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    invoice, item = _direct_invoice(session, company, accounts)
    received = _received(session, company, accounts, "991.67", date(2027, 2, 10), "zuerst")
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        bank_transaction_id=received.id,
        foreign_amount=Decimal("1190.00"),
        amount=Decimal("991.67"),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 3, 1),
        changed_by="t",
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2027, 3, 2),
        changed_by="t",
    )
    session.refresh(item)
    assert item.status == "settled"
    assert item.open_amount == ZERO
    assert _balance(session, accounts["1400"]) == ZERO


@pytest.mark.parametrize(
    ("foreign", "booked_rest"),
    [("1190.00", ZERO), ("595.00", Decimal("476.00"))],
    ids=["ganz bezahlt", "teilweise bezahlt"],
)
def test_valuation_has_no_row_for_a_cancelled_invoice(
    session: Session, foreign: str, booked_rest: Decimal
) -> None:
    """Hauptbuch 1400 = 0,00: Eine Bewertung über 1.190 USD wäre ein Ertrag ohne Grund."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    set_exchange_rate(
        session=session,
        company=company,
        currency_code="USD",
        rate_date=date(2027, 2, 28),
        rate="1.10",
        changed_by="t",
    )
    invoice, item = _direct_invoice(session, company, accounts)
    _pay(session, item.id, foreign, date(2027, 2, 5), bank_rate="1.20")
    session.refresh(item)
    assert item.open_amount == booked_rest
    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2027, 2, 10),
        changed_by="t",
    )
    assert fx_valuation_rows(session=session, company=company, as_of=date(2027, 2, 28)) == []
    assert _balance(session, accounts["1400"]) == ZERO


def test_third_booking_of_a_payment_stays_behind_the_second_reversal(
    session: Session, tmp_path: Path
) -> None:
    """Dreimal gebucht, zweimal storniert: Februar 158,33 €, März und April je 0,00 €.

    Der dritte Ausgleich läge sonst am 5.3. vor dem Storno vom 2.4., der März wäre 158,33 € zu hoch.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    issued = _invoice(session, company, accounts, tmp_path)
    terms = {
        "open_item_id": issued.open_item_id,
        "changed_by": "t",
        "foreign_amount": Decimal("1190.00"),
        "amount": Decimal("991.67"),
    }
    first = _manual_receipt(session, company, accounts, date(2027, 2, 10))
    settle_open_item(session=session, journal_entry_id=first.id, **terms)
    reverse_journal_entry(
        session=session, journal_entry_id=first.id, reversal_date=date(2027, 3, 5), changed_by="t"
    )
    second = _manual_receipt(session, company, accounts, date(2027, 2, 10))
    settle_open_item(session=session, journal_entry_id=second.id, **terms)
    reverse_journal_entry(
        session=session, journal_entry_id=second.id, reversal_date=date(2027, 4, 2), changed_by="t"
    )
    third = _manual_receipt(session, company, accounts, date(2027, 2, 10))
    settle_open_item(session=session, journal_entry_id=third.id, **terms)
    dates = [
        row.settlement_date
        for row in session.execute(
            select(OpenItemSettlement).order_by(OpenItemSettlement.id)
        ).scalars()
    ]
    assert dates == [date(2027, 2, 10), date(2027, 3, 5), date(2027, 4, 2)]
    assert _tax(session, company, "2027-02") == Decimal("158.33")
    assert _tax(session, company, "2027-03") == ZERO
    assert _tax(session, company, "2027-04") == ZERO


def test_real_revenue_on_the_fx_account_still_counts(session: Session, tmp_path: Path) -> None:
    """Die Ausnahme hängt an der Herkunft der Buchung, nicht am Konto."""
    company, accounts = _seed(session)
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 4, 5),
            description="Fehlbuchung auf das Kursertragskonto",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal("1190.00")),
                JournalLineInput(account_id=accounts["2660"].id, credit_amount=Decimal("1000.00")),
                JournalLineInput(account_id=accounts["1776"].id, credit_amount=Decimal("190.00")),
            ],
        ),
    )
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 4, 6),
            description="Ertrag ohne Umsatzsteuer",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal("50.00")),
                JournalLineInput(account_id=accounts["2660"].id, credit_amount=Decimal("50.00")),
            ],
        ),
    )
    kennzahlen, result = _kennzahlen(session, company, "2027-04")
    assert kennzahlen["81"] == Decimal("1000")
    assert kennzahlen["USt"] == Decimal("190.00")
    assert [item["amount"] for item in result.unassigned] == ["50.00"]


def test_reversed_fx_difference_is_not_named_as_revenue_either(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _, received, _ = _settled_full(session, company, accounts, tmp_path)
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    assert _balance(session, accounts["2660"]) == ZERO
    for period in ("2027-02", "2027-03"):
        assert _kennzahlen(session, company, period)[1].unassigned == []


def test_switch_back_is_free_when_payment_and_reversal_are_both_booked(
    session: Session, tmp_path: Path
) -> None:
    """Die Differenzen heben sich auf, 1776 steht wieder auf den gebuchten 152,00 €."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _, received, _ = _settled_full(session, company, accounts, tmp_path)
    _save(session, company, "2027-02")
    reverse_journal_entry(
        session=session,
        journal_entry_id=received.journal_entry_id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    _save(session, company, "2027-03")
    assert _balance(session, accounts["1776"]) == Decimal("-152.00")
    set_company_vat_method(
        session=session,
        company=company,
        method="soll",
        valid_from=date(2027, 1, 1),
        changed_by="t",
    )
    assert company.vat_cash_basis_from is None
    assert _tax(session, company, "2027-01") == Decimal("152.00")


def test_switch_back_to_accrual_refuses_when_a_difference_would_stay_behind(
    session: Session, tmp_path: Path
) -> None:
    """Wechsel zum 1.1.: Das Jahr zählte 152,00 €, auf 1776 stünden 158,33 €."""
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _settled_full(session, company, accounts, tmp_path)
    _save(session, company, "2027-02")
    with pytest.raises(VatReturnError, match="Steuerdifferenz"):
        set_company_vat_method(
            session=session,
            company=company,
            method="soll",
            valid_from=date(2027, 1, 1),
            changed_by="t",
        )
    assert company.vat_cash_basis_from == date(2027, 1, 1)
    # Ab später bleibt die Rechnung nach Ist; das bleibt möglich.
    set_company_vat_method(
        session=session,
        company=company,
        method="soll",
        valid_from=date(2027, 6, 1),
        changed_by="t",
    )
    assert company.vat_cash_basis_until == date(2027, 5, 31)


def test_invoice_rate_counts_exactly_what_is_booked(session: Session) -> None:
    """Kurs 0,5123 ohne Fremdbetrag an der Zeile: 10,39 € bleiben 10,39 €, nicht 10,38 €."""
    company, accounts = _seed(session)
    tax_code = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 1, 15),
            description="Rechnung",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(account_id=accounts["1400"].id, debit_amount=Decimal("65.08")),
                JournalLineInput(
                    account_id=accounts["8400"].id,
                    credit_amount=Decimal("54.69"),
                    tax_code_id=tax_code.id,
                ),
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    credit_amount=Decimal("10.39"),
                    tax_code_id=tax_code.id,
                ),
            ],
        ),
    )
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=accounts["1400"].id,
            item_type="receivable",
            reference="RE-KLEIN",
            entry_date=date(2027, 1, 15),
            amount=Decimal("0"),
            changed_by="t",
            foreign_currency_code="GBP",
            foreign_amount=Decimal("33.34"),
            exchange_rate=Decimal("0.5123"),
            journal_entry_id=entry.id,
        ),
    )
    _pay(session, item.id, "33.34", date(2027, 2, 10), bank_rate="0.60")
    kennzahlen, result = _kennzahlen(session, company, "2027-02")
    assert kennzahlen["USt"] == Decimal("10.39")
    event = result.foreign_payments[0]
    assert event.rate_missing is True
    assert event.tax == event.booked_tax == Decimal("10.39")


def test_fx_gain_is_not_named_as_revenue_without_tax(session: Session, tmp_path: Path) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _settled_full(session, company, accounts, tmp_path)
    assert _balance(session, accounts["2660"]) == Decimal("-39.67")
    result = _kennzahlen(session, company, "2027-02")[1]
    assert result.unassigned == []
    assert not any("Nicht in der UStVA" in warning for warning in result.warnings)


def test_datev_does_not_treat_the_fx_account_as_a_tax_base(session: Session) -> None:
    """Gegenläufig im Verhältnis 19 zu 100: +0,38 € auf 1776 aus einer, −2,00 € auf 1771 aus einer
    anderen Rechnung.

    Ohne Ausnahme liefen 2150 und 2660 im Stapel mit dem Schlüssel für 19 % Umsatzsteuer.
    """
    company, accounts = _seed(session)
    _monthly_rates(session, company, {"2027-02": "1.20"})
    tax_19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 2, 28),
            description="Umsatzsteuer-Kursdifferenz",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            source=SOURCE_VAT_FX_DIFFERENCE,
            lines=[
                JournalLineInput(account_id=tax_19.vat_account_id, debit_amount=Decimal("0.38")),
                JournalLineInput(account_id=accounts["1771"].id, credit_amount=Decimal("2.00")),
                JournalLineInput(account_id=accounts["2660"].id, credit_amount=Decimal("0.38")),
                JournalLineInput(account_id=accounts["2150"].id, debit_amount=Decimal("2.00")),
            ],
        ),
    )
    text_export = build_datev_export(
        session=session,
        company_id=company.id,
        generated_at=datetime(2028, 3, 1, tzinfo=timezone.utc),
    )
    rows = [row for row in text_export.splitlines() if entry.posting_number in row]
    assert rows
    assert not any('"101"' in row or '"102"' in row for row in rows)


def test_export_contains_the_rate_of_a_payment_inside_a_month(
    session: Session, tmp_path: Path
) -> None:
    company, accounts = _seed(session)
    _monthly_rates(session, company)
    _settled_full(session, company, accounts, tmp_path, day=date(2027, 2, 20))
    package = build_audit_export_package(
        session=session,
        company_id=company.id,
        generated_at=datetime(2028, 3, 1, tzinfo=timezone.utc),
        date_from=date(2027, 2, 15),
        date_to=date(2027, 2, 28),
        include_documents=False,
    )
    with zipfile.ZipFile(io.BytesIO(package.payload)) as archive:
        rates = json.loads(archive.read("data/vat_exchange_rates.json"))
        settlements = json.loads(archive.read("data/open_item_settlements.json"))
    assert {row["rate_month"] for row in rates} == {"2027-02-01"}
    assert settlements[0]["received_on"] == "2027-02-20"


@pytest.mark.parametrize("rate", ["1e30", "1e9", "100000000"])
def test_absurd_rates_are_refused_before_they_reach_the_database(
    session: Session, rate: str
) -> None:
    company, _ = _seed(session)
    with pytest.raises(ExchangeRateError, match="zu groß"):
        set_vat_exchange_rate(
            session=session,
            company=company,
            currency_code="USD",
            month="2027-03",
            rate=rate,
            changed_by="t",
        )
    with pytest.raises(ExchangeRateError, match="zu groß"):
        set_exchange_rate(
            session=session,
            company=company,
            currency_code="USD",
            rate_date=date(2027, 3, 1),
            rate=rate,
            changed_by="t",
        )


def test_annual_preview_and_saving_name_the_missing_rate(tmp_path: Path) -> None:
    app, company_id, _ = _api_app(tmp_path)
    client = app.test_client()
    with app.extensions["db_session_factory"]() as db:
        company = db.get(Company, company_id)
        accounts = {
            row.code: row
            for row in db.execute(select(Account).where(Account.company_id == company_id)).scalars()
        }
        issued = _invoice(db, company, accounts, tmp_path)
        _pay(db, issued.open_item_id, "595.00", date(2027, 2, 10))

    preview = client.get(
        "/api/v1/vat-annual-return",
        headers=READER,
        query_string={"company_id": company_id, "year": 2027},
    ).get_json()
    assert any("USD 2027-02" in warning for warning in preview["warnings"])
    assert preview["foreign_payments"][0]["rate_missing"] is True

    refused = client.post(
        "/api/v1/vat-annual-returns", headers=WRITER, json={"company_id": company_id, "year": 2027}
    )
    assert refused.status_code == 422
    assert "USD 2027-02" in refused.get_json()["error"]
    assert (
        client.post(
            "/api/v1/vat-annual-returns",
            headers=WRITER,
            json={"company_id": company_id, "year": "x"},
        ).status_code
        == 400
    )


def test_rest_tells_client_errors_from_source_errors(tmp_path: Path, monkeypatch) -> None:
    app, company_id, _ = _api_app(tmp_path)
    client = app.test_client()
    with app.extensions["db_session_factory"]() as db:
        franc_tenant = db.get(Company, company_id).tenant_id
        franc = Company(tenant_id=franc_tenant, name="Franken AG", currency_code="CHF")
        db.add(franc)
        db.commit()
        franc_id = franc.id
    _fake_bmf(monkeypatch, {})
    url = "/api/v1/vat-exchange-rates/fetch"
    assert (
        client.post(url, headers=WRITER, json={"company_id": company_id, "year": 2009}).status_code
        == 422
    )
    assert client.post(url, headers=WRITER, json={"company_id": franc_id}).status_code == 422
    assert client.post(url, headers=WRITER, json={"company_id": company_id}).status_code == 502
    assert (
        client.post(
            "/api/v1/vat-exchange-rates",
            headers=WRITER,
            json={
                "company_id": company_id,
                "currency_code": "USD",
                "month": "2027-02",
                "rate": "1e30",
            },
        ).status_code
        == 422
    )


def test_saved_returns_list_the_difference_entry_and_the_journal_page_labels_it(
    tmp_path: Path,
) -> None:
    app, company_id, _ = _api_app(tmp_path)
    client = app.test_client()
    with app.extensions["db_session_factory"]() as db:
        company = db.get(Company, company_id)
        accounts = {
            row.code: row
            for row in db.execute(select(Account).where(Account.company_id == company_id)).scalars()
        }
        _monthly_rates(db, company)
        issued = _invoice(db, company, accounts, tmp_path)
        _pay(db, issued.open_item_id, "595.00", date(2027, 2, 10))
        entry_id = _save(db, company, "2027-02").fx_journal_entry_id
    listed = client.get(
        "/api/v1/vat-returns", headers=READER, query_string={"company_id": company_id}
    ).get_json()["vat_returns"]
    assert listed[0]["fx_journal_entry_id"] == entry_id

    client.post("/auth/login", data={"username": "buchhalter", "password": "geheim123"})
    page = client.get(f"/buchungen/{entry_id}").get_data(as_text=True)
    assert "Umsatzsteuer-Kursdifferenz" in page
    assert ">Stornieren<" not in page
    assert "Ergänzen" not in page
    assert (
        "Ungültiges Jahr"
        in client.post(
            "/fremdwaehrung/bmf",
            data={"company_id": company_id, "year": "²"},
            follow_redirects=True,
        ).get_data(as_text=True)
    )


# ---------------------------------------------------------------------------
# BMF-Kurse
# ---------------------------------------------------------------------------


BMF_CSV = (
    "Monatlich fortgeschriebene Übersicht der Umsatzsteuer-Umrechnungskurse 2027 "
    "(Euro-Referenzkurse);;;;;;;;;;;;;\r\n"
    "Land;Währung;Januar[1];Februar [2];März [3];April [4];Mai [5];Juni [6];Juli [7];August [8];"
    "September [9];Oktober [10];November [11];Dezember [12]\r\n"
    "Indonesien;1 Euro;19.757,02 IDR;19.896,39 IDR;;;;;;;;;;\r\n"
    "Großbritannien;1 Euro;0,86828 GBP;0,87032 GBP;;;;;;;;;;\r\n"
    "Korea, Republik;1 Euro;1.710,38 KRW;1.712,39 RW;;;;;;;;;;\r\n"
    "Russland[1];1 Euro;;;;;;;;;;;;\r\n"
    "USA;1 Euro;1,1738 USD;1,1824 USD;;;;;;;;;;\r\n"
    ";;;;;;;;;;;;;\r\n"
    "[1] Die EZB hat die Veröffentlichung ausgesetzt.;;;;;;;;;;;;;\r\n"
).encode("cp1252")

BMF_CSV_OLD_STYLE = (
    "Gesamtübersicht 2017;;;;;;;;;;;;;\r\n"
    "Umsatzsteuer-Umrechnungskurse;;;;;;;;;;;;;\r\n"
    "Euro-Referenzkurse;;;;;;;;;;;;;\r\n"
    "Land;Währung;Jan 01;Feb 02;Mrz 03;Apr 04;Mai 05;Jun 06;Jul 07;Aug 08;Sep 09;Okt 10;Nov 11;"
    "Dez 12\r\n"
    'Australien;1 Euro;"1,4252\nAUD";"1,3923\nAUD";"1,3969\nAUD";;;;;;;;;\r\n'
).encode("cp1252")


def test_parse_bmf_rates_reads_the_file_as_the_bmf_writes_it() -> None:
    rates, ignored = parse_bmf_rates(BMF_CSV, year=2027)
    by_key = {(month, code): rate for month, code, rate in rates}
    assert by_key[(date(2027, 1, 1), "USD")] == Decimal("1.173800")
    assert by_key[(date(2027, 2, 1), "IDR")] == Decimal("19896.390000")
    assert by_key[(date(2027, 1, 1), "GBP")] == Decimal("0.868280")
    assert by_key[(date(2027, 1, 1), "KRW")] == Decimal("1710.380000")
    # „RW“ statt „KRW“ wird nicht geraten: Der Monat fehlt und wird gezählt.
    assert (date(2027, 2, 1), "KRW") not in by_key
    assert ignored == 1
    assert len(rates) == 7

    old, ignored = parse_bmf_rates(BMF_CSV_OLD_STYLE, year=2017)
    assert ignored == 0
    assert {(month, rate) for month, _, rate in old} == {
        (date(2017, 1, 1), Decimal("1.425200")),
        (date(2017, 2, 1), Decimal("1.392300")),
        (date(2017, 3, 1), Decimal("1.396900")),
    }


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"<html>Seite nicht gefunden</html>", "nicht lesbar"),
        (b"Land;1 Euro;Januar;Februar\r\n", "zwölf Monate"),
        (BMF_CSV_OLD_STYLE, "nicht zum Jahr 2027"),
    ],
)
def test_parse_bmf_rates_rejects_other_files(content: bytes, message: str) -> None:
    with pytest.raises(ExchangeRateError, match=message):
        parse_bmf_rates(content, year=2027)


def test_fetch_bmf_rates_keeps_manual_rates_and_takes_corrections(session: Session) -> None:
    company, _ = _seed(session)
    current = date.today().year
    content = BMF_CSV.replace(b"2027", str(current).encode())
    set_vat_exchange_rate(
        session=session,
        company=company,
        currency_code="USD",
        month=f"{current}-01",
        rate="1.50",
        changed_by="t",
    )

    # Das Vorjahr gibt es, das laufende Jahr noch nicht oder umgekehrt: Beides reicht.
    requested = []

    def download(year):
        requested.append(year)
        return content if year == current else None

    first = fetch_bmf_rates(session=session, company=company, changed_by="t", download=download)
    assert requested == [current - 1, current]
    assert first["created"] == 6
    assert first["updated"] == 0
    assert first["skipped"] == 1
    assert first["ignored"] == 1
    assert first["years"] == [current]
    assert first["latest_month"] == date(current, 2, 1)

    rows = list_vat_exchange_rates(
        session=session, tenant_id=company.tenant_id, currency_code="USD"
    )
    assert [(row.rate_month.month, row.rate, row.source) for row in rows] == [
        (2, Decimal("1.182400"), "bmf"),
        (1, Decimal("1.500000"), "manual"),
    ]

    corrected = content.replace(b"1,1824 USD", b"1,2000 USD")
    second = fetch_bmf_rates(
        session=session,
        company=company,
        changed_by="t",
        download=lambda year: corrected if year == current else None,
    )
    assert second["created"] == 0
    assert second["updated"] == 1
    session.expire_all()
    assert rows[0].rate == Decimal("1.200000")
    assert rows[1].rate == Decimal("1.500000")
    assert session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "vat_exchange_rate", AuditLog.action == "fetched"
        )
    ).scalars().all()


def test_fetch_bmf_rates_names_what_is_wrong(session: Session) -> None:
    company, _ = _seed(session)
    with pytest.raises(ExchangeRateError, match="keine Kursdatei"):
        fetch_bmf_rates(
            session=session, company=company, changed_by="t", download=lambda year: None
        )
    for year in (2009, date.today().year + 1):
        with pytest.raises(ExchangeRateError, match="von 2010 bis"):
            fetch_bmf_rates(
                session=session,
                company=company,
                changed_by="t",
                year=year,
                download=lambda _: BMF_CSV,
            )


def test_set_vat_exchange_rate_validates_and_normalizes(session: Session) -> None:
    company, _ = _seed(session)
    assert normalize_rate_month("2027-03") == date(2027, 3, 1)
    assert normalize_rate_month(date(2027, 3, 17)) == date(2027, 3, 1)
    for month in ("März", "2027-13", ""):
        with pytest.raises(ExchangeRateError, match="Ungültiger Monat"):
            normalize_rate_month(month)
    for currency, rate, message in (
        ("US", "1.08", "drei Buchstaben"),
        ("EUR", "1.08", "Firmenwährung"),
        ("USD", "0", "größer 0"),
        ("USD", "abc", "Ungültiger Kurs"),
    ):
        with pytest.raises(ExchangeRateError, match=message):
            set_vat_exchange_rate(
                session=session,
                company=company,
                currency_code=currency,
                month="2027-03",
                rate=rate,
                changed_by="t",
            )
    first = set_vat_exchange_rate(
        session=session,
        company=company,
        currency_code="usd",
        month="2027-03-17",
        rate="1,08",
        changed_by="t",
    )
    assert (first.currency_code, first.rate_month, first.rate, first.source) == (
        "USD",
        date(2027, 3, 1),
        Decimal("1.080000"),
        "manual",
    )
    assert EXPORT_TABLE_MODELS["vat_exchange_rates"] is VatExchangeRate


def test_rates_are_separate_per_tenant(session: Session) -> None:
    company, _ = _seed(session)
    other_tenant = Tenant(name="Andere")
    other = Company(tenant=other_tenant, name="Andere GmbH", currency_code="EUR")
    session.add_all([other_tenant, other])
    session.commit()
    _monthly_rates(session, company, {"2027-02": "1.20"})
    _monthly_rates(session, other, {"2027-02": "1.40"})
    assert [
        row.rate for row in list_vat_exchange_rates(session=session, tenant_id=company.tenant_id)
    ] == [Decimal("1.200000")]
    assert [
        row.rate for row in list_vat_exchange_rates(session=session, tenant_id=other.tenant_id)
    ] == [Decimal("1.400000")]


# ---------------------------------------------------------------------------
# REST, MCP, Oberfläche, CLI, Migration
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self.content


def _fake_bmf(monkeypatch, files: dict[int, bytes]) -> list[str]:
    """Das Datenportal als Attrappe; 404 für Jahre ohne Datei wie beim echten Portal."""
    requested: list[str] = []

    def urlopen(request, timeout=None):
        url = request.full_url
        requested.append(url)
        assert request.get_header("User-agent") == "OpenBuchhaltung"
        assert timeout
        for year, content in files.items():
            if f"uu-kurse-{year}-csv.csv?__blob=publicationFile" in url:
                return _Response(content)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requested


def _this_year_file() -> tuple[int, bytes]:
    current = date.today().year
    return current, BMF_CSV.replace(b"2027", str(current).encode())


READER = {"Authorization": "Bearer obk_pruefer-mk"}
WRITER = {"Authorization": "Bearer obk_buchhalter-mk"}


def _api_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "API_REQUIRE_AUTH": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'mk.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db:
        company, _ = _seed(db)
        foreign_tenant = Tenant(name="Fremd")
        foreign = Company(tenant=foreign_tenant, name="Fremd GmbH", currency_code="EUR")
        db.add_all([foreign_tenant, foreign])
        db.flush()
        for username, role, token in (
            ("pruefer", "Pruefer", "obk_pruefer-mk"),
            ("buchhalter", "Buchhalter", "obk_buchhalter-mk"),
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
        return app, company.id, foreign.id


def test_rest_covers_monthly_rates(tmp_path: Path, monkeypatch) -> None:
    app, company_id, foreign_id = _api_app(tmp_path)
    client = app.test_client()
    url = "/api/v1/vat-exchange-rates"
    body = {"company_id": company_id, "currency_code": "USD", "month": "2027-02", "rate": "1.20"}

    assert client.post(url, headers=READER, json=body).status_code == 403
    assert client.post(url, headers=WRITER, json={**body, "rate": "0"}).status_code == 422
    assert client.post(url, headers=WRITER, json={**body, "month": "x"}).status_code == 422
    assert (
        client.post(url, headers=WRITER, json={**body, "company_id": foreign_id}).status_code
        == 404
    )
    created = client.post(url, headers=WRITER, json=body)
    assert created.status_code == 201
    assert created.get_json()["month"] == "2027-02"
    assert created.get_json()["rate"] == "1.200000"
    assert created.get_json()["source"] == "manual"

    listed = client.get(
        url, headers=READER, query_string={"company_id": company_id, "currency_code": "USD"}
    ).get_json()["vat_exchange_rates"]
    assert [row["month"] for row in listed] == ["2027-02"]
    assert (
        client.get(
            url, headers=READER, query_string={"company_id": company_id, "month_from": "x"}
        ).status_code
        == 400
    )
    assert (
        client.get(url, headers=READER, query_string={"company_id": foreign_id}).status_code == 404
    )

    current, content = _this_year_file()
    requested = _fake_bmf(monkeypatch, {current: content})
    fetch_url = "/api/v1/vat-exchange-rates/fetch"
    assert (
        client.post(fetch_url, headers=READER, json={"company_id": company_id}).status_code == 403
    )
    assert (
        client.post(
            fetch_url, headers=WRITER, json={"company_id": company_id, "year": "x"}
        ).status_code
        == 400
    )
    fetched = client.post(
        fetch_url, headers=WRITER, json={"company_id": company_id, "year": current}
    )
    assert fetched.status_code == 200, fetched.get_json()
    assert fetched.get_json()["created"] == 7
    assert fetched.get_json()["latest_month"] == f"{current}-02"
    assert len(requested) == 1
    assert requested[0].startswith("https://www.bundesfinanzministerium.de/Datenportal/")
    # Ohne Jahr auch das Vorjahr; es fehlt hier, das stört nicht.
    assert (
        client.post(fetch_url, headers=WRITER, json={"company_id": company_id}).status_code == 200
    )
    assert len(requested) == 3
    # Ein Jahr, das das Portal nicht hat, und ein Ausfall des Portals sind Fehler.
    _fake_bmf(monkeypatch, {})
    missing = client.post(fetch_url, headers=WRITER, json={"company_id": company_id})
    assert missing.status_code == 502
    assert "keine Kursdatei" in missing.get_json()["error"]

    def unreachable(request, timeout=None):
        raise urllib.error.URLError("keine Verbindung")

    monkeypatch.setattr("urllib.request.urlopen", unreachable)
    down = client.post(fetch_url, headers=WRITER, json={"company_id": company_id})
    assert down.status_code == 502
    assert "nicht abgerufen" in down.get_json()["error"]

    class _Broken:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"Land;")

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=None: _Broken())
    broken = client.post(fetch_url, headers=WRITER, json={"company_id": company_id})
    assert broken.status_code == 502


def test_vat_return_api_names_the_payments(tmp_path: Path) -> None:
    app, company_id, _ = _api_app(tmp_path)
    client = app.test_client()
    with app.extensions["db_session_factory"]() as db:
        company = db.get(Company, company_id)
        accounts = {
            row.code: row
            for row in db.execute(select(Account).where(Account.company_id == company_id)).scalars()
        }
        _monthly_rates(db, company)
        issued = _invoice(db, company, accounts, tmp_path)
        _pay(db, issued.open_item_id, "595.00", date(2027, 2, 10))
    payload = client.get(
        "/api/v1/vat-return",
        headers=READER,
        query_string={"company_id": company_id, "period": "2027-02"},
    ).get_json()
    assert payload["foreign_payments"] == [
        {
            "reference": payload["foreign_payments"][0]["reference"],
            "kind": "payment",
            "currency_code": "USD",
            "month": "2027-02",
            "rate": "1.200000",
            "rate_missing": False,
            "foreign_amount": "595.00",
            "tax": "79.17",
            "booked_tax": "76.00",
            "difference": "-3.17",
            "posted": False,
        }
    ]
    saved = client.post(
        "/api/v1/vat-returns", headers=WRITER, json={"company_id": company_id, "period": "2027-02"}
    )
    assert saved.status_code == 201, saved.get_json()
    assert saved.get_json()["fx_journal_entry_id"] is not None
    posted = client.get(
        "/api/v1/vat-return",
        headers=READER,
        query_string={"company_id": company_id, "period": "2027-02"},
    ).get_json()
    assert posted["foreign_payments"][0]["posted"] is True


def test_missing_rate_blocks_saving_through_the_api(tmp_path: Path) -> None:
    app, company_id, _ = _api_app(tmp_path)
    client = app.test_client()
    with app.extensions["db_session_factory"]() as db:
        company = db.get(Company, company_id)
        accounts = {
            row.code: row
            for row in db.execute(select(Account).where(Account.company_id == company_id)).scalars()
        }
        issued = _invoice(db, company, accounts, tmp_path)
        _pay(db, issued.open_item_id, "595.00", date(2027, 2, 10))
    response = client.post(
        "/api/v1/vat-returns", headers=WRITER, json={"company_id": company_id, "period": "2027-02"}
    )
    assert response.status_code == 422
    assert "USD 2027-02" in response.get_json()["error"]


def test_ui_shows_and_records_monthly_rates(tmp_path: Path, monkeypatch) -> None:
    app, company_id, _ = _api_app(tmp_path)
    with app.extensions["db_session_factory"]() as db:
        company = db.get(Company, company_id)
        accounts = {
            row.code: row
            for row in db.execute(select(Account).where(Account.company_id == company_id)).scalars()
        }
        issued = _invoice(db, company, accounts, tmp_path)
        _pay(db, issued.open_item_id, "595.00", date(2027, 2, 10))
    client = app.test_client()
    assert (
        client.post(
            "/auth/login", data={"username": "buchhalter", "password": "geheim123"}
        ).status_code
        == 302
    )
    form = {"company_id": company_id}

    rate_form = {**form, "currency_code": "USD", "month": "2027-02", "rate": "1,20"}
    assert client.post("/fremdwaehrung/monatskurs", data=rate_form).status_code == 302
    page = client.get("/fremdwaehrung", query_string={"company_id": company_id}).get_data(
        as_text=True
    )
    assert "02/2027" in page
    assert "BMF-Kurse abrufen" in page
    assert "1,2" in page

    current, content = _this_year_file()
    _fake_bmf(monkeypatch, {current: content})
    fetched = client.post(
        "/fremdwaehrung/bmf", data={**form, "year": str(current)}, follow_redirects=True
    ).get_data(as_text=True)
    assert "7 neue und 0 geänderte Monatskurse" in fetched
    assert "1 Angaben der BMF-Datei" in fetched
    bad_year = client.post(
        "/fremdwaehrung/bmf", data={**form, "year": "x"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Ungültiges Jahr" in bad_year

    ustva = client.get(
        "/ustva", query_string={"company_id": company_id, "period": "2027-02"}
    ).get_data(as_text=True)
    assert "Zahlungen auf Rechnungen in Fremdwährung" in ustva
    assert "595,00 USD" in ustva
    assert "79,17" in ustva
    saved = client.post(
        "/ustva/festhalten", data={**form, "period": "2027-02"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "auf dem Steuerkonto gebucht" in saved


def test_cli_fetches_for_each_tenant_once(tmp_path: Path, monkeypatch) -> None:
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"}
    )
    with app.extensions["db_session_factory"]() as db:
        company, _ = _seed(db)
        db.add(Company(tenant_id=company.tenant_id, name="Zweite GmbH", currency_code="EUR"))
        db.commit()
    current, content = _this_year_file()
    _fake_bmf(monkeypatch, {current: content})
    result = app.test_cli_runner().invoke(
        args=["fetch-vat-exchange-rates", "--year", str(current)]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("7 neue") == 1

    _fake_bmf(monkeypatch, {})
    failed = app.test_cli_runner().invoke(args=["fetch-vat-exchange-rates"])
    assert failed.exit_code != 0
    assert "keine Kursdatei" in failed.output


def test_migration_keeps_reported_payments_at_the_invoice_rate(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'mk-migration.db'}"
    session_factory = create_session_factory(url)
    with session_factory() as db:
        company, accounts = _seed(db)
        issued = _invoice(db, company, accounts, tmp_path)
        _pay(db, issued.open_item_id, "595.00", date(2027, 2, 10))
        _monthly_rates(db, company)
        saved = _save(db, company, "2027-02")
        reported = saved.kennzahlen
        _pay(db, issued.open_item_id, "238.00", date(2027, 3, 10))
        company_id = company.id
        db.execute(text("UPDATE open_item_settlement SET vat_rate = NULL, vat_fx_entry_id = NULL"))
        db.commit()

    engine = create_engine(url)
    config = _alembic_config(engine)
    command.downgrade(config, "20261104_0065")
    tables = inspect(engine)
    assert "vat_exchange_rate" not in tables.get_table_names()
    assert "vat_rate" not in {
        column["name"] for column in tables.get_columns("open_item_settlement")
    }
    assert "fx_journal_entry_id" not in {
        column["name"] for column in tables.get_columns("vat_return")
    }

    command.upgrade(config, "head")
    with engine.connect() as connection:
        pinned = connection.execute(
            text("SELECT settlement_date, vat_rate FROM open_item_settlement ORDER BY id")
        ).all()
    # Die gemeldete Februar-Zahlung behält den Rechnungskurs, die spätere bleibt offen.
    assert str(pinned[0][1]).startswith("1.25")
    assert pinned[1][1] is None
    with Session(engine) as db:
        assert db.get(Company, company_id) is not None
        assert db.execute(select(VatReturn)).scalar_one().kennzahlen == reported
        # Zum Rechnungskurs: 95,00 USD / 1,25 = 76,00 €, und damit keine Differenz.
        result = compute_vat_return_details(
            session=db,
            company_id=company_id,
            date_from=date(2027, 2, 1),
            date_to=date(2027, 2, 28),
        )
        assert {row.kennziffer: row.amount for row in result.rows}["USt"] == Decimal("76.00")
        assert result.foreign_payments[0].booked_tax == result.foreign_payments[0].tax


def test_migration_also_keeps_a_reported_reversal_at_the_invoice_rate(tmp_path: Path) -> None:
    """Zahlung ungemeldet, Storno gemeldet zum Rechnungskurs: −152,00 € bleiben −152,00 €.

    Zum Monatskurs wäre die Neuberechnung −158,33 €, das Jahr passte nicht mehr zu den Meldungen.
    """
    url = f"sqlite+pysqlite:///{tmp_path / 'mk-storno.db'}"
    session_factory = create_session_factory(url)
    with session_factory() as db:
        company, accounts = _seed(db)
        _monthly_rates(db, company)
        _, received, _ = _settled_full(db, company, accounts, tmp_path)
        reverse_journal_entry(
            session=db,
            journal_entry_id=received.journal_entry_id,
            reversal_date=date(2027, 3, 5),
            changed_by="t",
        )
        db.add(
            VatReturn(
                tenant_id=company.tenant_id,
                company_id=company.id,
                period_label="2027-03",
                date_from=date(2027, 3, 1),
                date_to=date(2027, 3, 31),
                kennzahlen=[{"kennziffer": "USt", "label": "USt", "amount": "-152.00"}],
                status="erstellt",
                created_by="t",
            )
        )
        db.commit()
        company_id = company.id

    engine = create_engine(url)
    config = _alembic_config(engine)
    command.downgrade(config, "20261104_0065")
    command.upgrade(config, "head")
    with Session(engine) as db:
        for period, tax in (("2027-02", Decimal("152.00")), ("2027-03", Decimal("-152.00"))):
            start, end, _ = period_bounds(period)
            rows = compute_vat_return_details(
                session=db, company_id=company_id, date_from=start, date_to=end
            ).rows
            assert {row.kennziffer: row.amount for row in rows}["USt"] == tax
