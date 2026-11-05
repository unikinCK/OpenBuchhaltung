"""Istversteuerung: UStVA nach Zahlung, Zahlungstabelle, Storno und Wechsel der Versteuerungsart."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, create_session_factory
from app.services.audit_export import EXPORT_TABLE_MODELS
from app.services.bank_import import book_transaction
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.open_items import OpenItemInput, create_open_item, settle_open_item
from app.services.tax_codes import ensure_default_tax_codes
from app.services.vat_returns import (
    VatReturnError,
    company_vat_method,
    compute_vat_return_details,
    save_vat_return,
    set_company_vat_method,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    OpenItem,
    OpenItemSettlement,
    TaxCode,
    Tenant,
    User,
)

SKR03 = (
    ("1200", "Bank", "asset", None),
    ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
    ("1576", "Abziehbare Vorsteuer 19 %", "asset", None),
    ("1776", "Umsatzsteuer 19 %", "liability", None),
    ("1771", "Umsatzsteuer 7 %", "liability", None),
    ("1571", "Abziehbare Vorsteuer 7 %", "asset", None),
    ("8400", "Erlöse 19 % USt", "income", None),
    ("8125", "Steuerfreie innergemeinschaftliche Lieferungen", "income", None),
    ("4210", "Miete", "expense", None),
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name="Ist Mandant")
    company = Company(tenant=tenant, name="Ist GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts: dict[str, Account] = {}
    for code, name, account_type, subledger in SKR03:
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


def _tax_code(session, company, code):
    return session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == code)
    ).scalar_one()


def _invoice(session, company, accounts, *, gross="119.00", day=date(2027, 1, 15), with_item=True):
    net = (Decimal(gross) / Decimal("1.19")).quantize(Decimal("0.01"))
    tax_code = _tax_code(session, company, "USt19")
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description="Rechnung",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(account_id=accounts["1400"].id, debit_amount=Decimal(gross)),
                JournalLineInput(
                    account_id=accounts["8400"].id, credit_amount=net, tax_code_id=tax_code.id
                ),
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    credit_amount=Decimal(gross) - net,
                    tax_code_id=tax_code.id,
                ),
            ],
        ),
    )
    item = None
    if with_item:
        item = create_open_item(
            session=session,
            payload=OpenItemInput(
                company_id=company.id,
                account_id=accounts["1400"].id,
                item_type="receivable",
                reference=f"RE-{entry.id}",
                entry_date=day,
                amount=Decimal(gross),
                journal_entry_id=entry.id,
                changed_by="t",
            ),
        )
    return entry, item


def _payment(session, company, accounts, amount, day):
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


def _kennzahlen(session, company, date_from, date_to) -> dict[str, Decimal]:
    result = compute_vat_return_details(
        session=session, company_id=company.id, date_from=date_from, date_to=date_to
    )
    return {row.kennziffer: row.amount for row in result.rows}


def _set_cash_basis(session, company, day=date(2027, 1, 1)):
    set_company_vat_method(
        session=session, company=company, method="ist", valid_from=day, changed_by="t"
    )


def test_rebooked_payment_does_not_count_twice(session: Session) -> None:
    """Storno und Neubuchung desselben Umsatzes ändern den gemeldeten Februar nicht."""
    company, accounts = _seed(session)
    _set_cash_basis(session, company, date(2027, 1, 1))
    _, item = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 15))
    transaction = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=accounts["1200"].id,
        booking_date=date(2027, 2, 10),
        amount=Decimal("59.50"),
        currency_code="EUR",
        purpose="Teilzahlung",
        status="open",
        dedup_hash="teilzahlung",
    )
    session.add(transaction)
    session.commit()
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    february = (date(2027, 2, 1), date(2027, 2, 28))
    march = (date(2027, 3, 1), date(2027, 3, 31))
    assert _kennzahlen(session, company, *february)["83"] == Decimal("9.50")

    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2027, 3, 5),
        changed_by="t",
    )
    session.refresh(item)
    assert item.status == "open"
    assert _kennzahlen(session, company, *march)["83"] == Decimal("-9.50")

    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    assert _kennzahlen(session, company, *february)["83"] == Decimal("9.50")
    assert _kennzahlen(session, company, *march).get("83", Decimal("0.00")) == Decimal("0.00")


def _booked_receipt(session, company, accounts, amount, day, tag):
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


def test_payment_moved_to_another_item_stays_in_the_later_period(session: Session) -> None:
    """Falsch zugeordnet und korrigiert: Der gemeldete Februar bleibt, der April geht auf."""
    company, accounts = _seed(session)
    _set_cash_basis(session, company, date(2027, 1, 1))
    _, wrong = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 15))
    _, right = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 16))
    transaction = _booked_receipt(
        session, company, accounts, "119.00", date(2027, 2, 10), "falscher-posten"
    )
    settle_open_item(
        session=session, open_item_id=wrong.id, changed_by="t", bank_transaction_id=transaction.id
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(
        session=session, open_item_id=right.id, changed_by="t", bank_transaction_id=transaction.id
    )
    assert _kennzahlen(session, company, date(2027, 2, 1), date(2027, 2, 28))["83"] == Decimal(
        "19.00"
    )
    assert _kennzahlen(session, company, date(2027, 4, 1), date(2027, 4, 30)).get(
        "83", Decimal("0.00")
    ) == Decimal("0.00")


def test_second_reversal_dated_before_the_settlement_keeps_the_sum(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company, date(2027, 1, 1))
    _, item = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 15))
    transaction = _booked_receipt(session, company, accounts, "59.50", date(2027, 2, 10), "zweimal")
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2027, 4, 5),
        changed_by="t",
    )
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["1400"].id,
        changed_by="t",
    )
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2027, 3, 20),
        changed_by="t",
    )
    total = sum(
        (
            _kennzahlen(session, company, start, end).get("83", Decimal("0.00"))
            for start, end in (
                (date(2027, 2, 1), date(2027, 2, 28)),
                (date(2027, 3, 1), date(2027, 3, 31)),
                (date(2027, 4, 1), date(2027, 4, 30)),
            )
        ),
        Decimal("0.00"),
    )
    assert total == Decimal("0.00")


def test_reversal_dated_in_a_reported_month_counts_in_the_next_open_one(session: Session) -> None:
    """Februar bleibt wie gemeldet, die 19,00 € Korrektur stehen im März; auch in Euro."""
    company, accounts = _seed(session)
    _set_cash_basis(session, company, date(2027, 1, 1))
    _, item = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 15))
    transaction = _booked_receipt(session, company, accounts, "119.00", date(2027, 2, 10), "weg")
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    save_vat_return(
        session=session, company_id=company.id, period_label="2027-02", changed_by="t"
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2027, 2, 20),
        changed_by="t",
    )
    february = (date(2027, 2, 1), date(2027, 2, 28))
    march = (date(2027, 3, 1), date(2027, 3, 31))
    assert _kennzahlen(session, company, *february)["83"] == Decimal("19.00")
    assert _kennzahlen(session, company, *march)["83"] == Decimal("-19.00")


def test_paid_invoice_stays_until_its_payment_is_reversed(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company, date(2027, 1, 1))
    entry, item = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 15))
    transaction = _booked_receipt(session, company, accounts, "119.00", date(2027, 2, 10), "bleibt")
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    with pytest.raises(JournalEntryCreationError, match="zuerst die Zahlung stornieren"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=entry.id,
            reversal_date=date(2027, 3, 1),
            changed_by="t",
        )


def test_payment_assigned_after_the_return_is_named(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company, date(2027, 1, 1))
    _, item = _invoice(session, company, accounts, gross="119.00", day=date(2027, 1, 15))
    transaction = _booked_receipt(session, company, accounts, "119.00", date(2027, 2, 10), "spaet")
    february = {"date_from": date(2027, 2, 1), "date_to": date(2027, 2, 28)}
    save_vat_return(
        session=session, company_id=company.id, period_label="2027-02", changed_by="t"
    )
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", bank_transaction_id=transaction.id
    )
    result = compute_vat_return_details(session=session, company_id=company.id, **february)
    assert any(item.reference in warning and "2027-02" in warning for warning in result.warnings)


def test_accrual_method_stays_unchanged(session: Session) -> None:
    company, accounts = _seed(session)
    _invoice(session, company, accounts)
    values = _kennzahlen(session, company, date(2027, 1, 1), date(2027, 1, 31))
    assert values["81"] == Decimal("100")
    assert values["83"] == Decimal("19.00")


def test_invoice_counts_when_paid(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company)
    _, item = _invoice(session, company, accounts)

    january = compute_vat_return_details(
        session=session,
        company_id=company.id,
        date_from=date(2027, 1, 1),
        date_to=date(2027, 1, 31),
    )
    values = {row.kennziffer: row.amount for row in january.rows}
    assert values.get("81", Decimal(0)) == Decimal(0)
    assert values.get("83", Decimal(0)) == Decimal("0.00")
    assert any("19,00 €" in warning and "nicht fällig" in warning for warning in january.warnings)

    payment = _payment(session, company, accounts, "119.00", date(2027, 2, 10))
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
    )
    assert _kennzahlen(session, company, date(2027, 1, 1), date(2027, 1, 31)).get(
        "83", Decimal(0)
    ) == Decimal("0.00")
    february = _kennzahlen(session, company, date(2027, 2, 1), date(2027, 2, 28))
    assert february["81"] == Decimal("100")
    assert february["83"] == Decimal("19.00")


def test_partial_payments_add_up_to_the_invoice_without_rounding_rest(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company)
    _, item = _invoice(session, company, accounts, gross="100.00")
    for amount, day in (("33.33", date(2027, 2, 1)), ("33.33", date(2027, 3, 1))):
        payment = _payment(session, company, accounts, amount, day)
        settle_open_item(
            session=session,
            open_item_id=item.id,
            changed_by="t",
            journal_entry_id=payment.id,
            amount=Decimal(amount),
        )
    payment = _payment(session, company, accounts, "33.34", date(2027, 4, 1))
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
    )

    total = sum(
        _kennzahlen(session, company, date(2027, month, 1), date(2027, month, 28)).get(
            "83", Decimal(0)
        )
        for month in (2, 3, 4)
    )
    assert total == Decimal("15.97")
    year = _kennzahlen(session, company, date(2027, 1, 1), date(2027, 12, 31))
    assert year["83"] == Decimal("15.97")
    assert year["81"] == Decimal("84")


def test_reversed_payment_reduces_the_period_of_the_reversal(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company)
    _, item = _invoice(session, company, accounts)
    payment = _payment(session, company, accounts, "119.00", date(2027, 2, 10))
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        changed_by="t",
        reversal_date=date(2027, 3, 5),
    )
    session.refresh(item)
    assert item.status == "open"
    assert item.open_amount == Decimal("119.00")
    settlement = session.execute(select(OpenItemSettlement)).scalar_one()
    assert settlement.reversed_on == date(2027, 3, 5)

    # Der Februar bleibt wie gemeldet, die Korrektur steht im März.
    assert _kennzahlen(session, company, date(2027, 2, 1), date(2027, 2, 28))["83"] == Decimal(
        "19.00"
    )
    assert _kennzahlen(session, company, date(2027, 3, 1), date(2027, 3, 31))["83"] == Decimal(
        "-19.00"
    )


def test_reversing_a_payment_restores_every_settled_item(session: Session) -> None:
    company, accounts = _seed(session)
    _, first = _invoice(session, company, accounts, gross="50.00")
    _, second = _invoice(session, company, accounts, gross="70.00")
    payment = _payment(session, company, accounts, "120.00", date(2027, 2, 10))
    for item in (first, second):
        settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )
    reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        changed_by="t",
        reversal_date=date(2027, 3, 5),
    )
    for item in (first, second):
        session.refresh(item)
        assert item.status == "open"
        assert item.open_amount == item.original_amount


def test_unlinked_invoice_counts_on_booking_date_with_warning(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company)
    entry, _ = _invoice(session, company, accounts, with_item=False)
    result = compute_vat_return_details(
        session=session,
        company_id=company.id,
        date_from=date(2027, 1, 1),
        date_to=date(2027, 1, 31),
    )
    values = {row.kennziffer: row.amount for row in result.rows}
    assert values["83"] == Decimal("19.00")
    assert any(entry.posting_number in warning for warning in result.warnings)


def test_input_tax_and_direct_sales_stay_on_their_dates(session: Session) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company)
    input_code = _tax_code(session, company, "VSt19")
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 1, 10),
            description="Eingangsrechnung",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(
                    account_id=accounts["4210"].id,
                    debit_amount=Decimal("100.00"),
                    tax_code_id=input_code.id,
                ),
                JournalLineInput(
                    account_id=input_code.vat_account_id,
                    debit_amount=Decimal("19.00"),
                    tax_code_id=input_code.id,
                ),
                JournalLineInput(account_id=accounts["1200"].id, credit_amount=Decimal("119.00")),
            ],
        ),
    )
    output_code = _tax_code(session, company, "USt19")
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2027, 1, 12),
            description="Barverkauf",
            status="posted",
            changed_by="t",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(account_id=accounts["1200"].id, debit_amount=Decimal("238.00")),
                JournalLineInput(
                    account_id=accounts["8400"].id,
                    credit_amount=Decimal("200.00"),
                    tax_code_id=output_code.id,
                ),
                JournalLineInput(
                    account_id=output_code.vat_account_id,
                    credit_amount=Decimal("38.00"),
                    tax_code_id=output_code.id,
                ),
            ],
        ),
    )
    values = _kennzahlen(session, company, date(2027, 1, 1), date(2027, 1, 31))
    assert values["66"] == Decimal("19.00")
    assert values["81"] == Decimal("200")
    assert values["83"] == Decimal("19.00")


def test_invoice_before_cash_basis_stays_on_booking_date(session: Session) -> None:
    company, accounts = _seed(session)
    _, item = _invoice(session, company, accounts, day=date(2026, 12, 20))
    _set_cash_basis(session, company, date(2027, 1, 1))
    assert _kennzahlen(session, company, date(2026, 12, 1), date(2026, 12, 31))["83"] == Decimal(
        "19.00"
    )
    payment = _payment(session, company, accounts, "119.00", date(2027, 1, 20))
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
    )
    assert _kennzahlen(session, company, date(2027, 1, 1), date(2027, 1, 31)).get(
        "83", Decimal(0)
    ) == Decimal("0.00")


def test_invoice_from_cash_basis_period_paid_after_its_end_still_counts_at_payment(
    session: Session,
) -> None:
    company, accounts = _seed(session)
    _set_cash_basis(session, company)
    _, item = _invoice(session, company, accounts)
    set_company_vat_method(
        session=session,
        company=company,
        method="soll",
        valid_from=date(2027, 3, 1),
        changed_by="t",
    )
    payment = _payment(session, company, accounts, "119.00", date(2027, 4, 10))
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
    )
    assert _kennzahlen(session, company, date(2027, 4, 1), date(2027, 4, 30))["83"] == Decimal(
        "19.00"
    )
    assert company_vat_method(company, on=date(2027, 2, 1))["method"] == "ist"
    assert company_vat_method(company, on=date(2027, 3, 1))["method"] == "soll"


def test_settlement_date_falls_back_to_bank_and_today(session: Session) -> None:
    company, accounts = _seed(session)
    _, item = _invoice(session, company, accounts)
    settle_open_item(
        session=session,
        open_item_id=item.id,
        changed_by="t",
        amount=Decimal("10.00"),
        settlement_date=date(2027, 2, 2),
    )
    settle_open_item(
        session=session, open_item_id=item.id, changed_by="t", amount=Decimal("10.00")
    )
    rows = session.execute(
        select(OpenItemSettlement).order_by(OpenItemSettlement.id)
    ).scalars().all()
    assert [row.settlement_date for row in rows] == [date(2027, 2, 2), date.today()]
    assert [row.amount for row in rows] == [Decimal("10.00"), Decimal("10.00")]


def test_vat_method_rules_and_audit(session: Session) -> None:
    company, accounts = _seed(session)
    with pytest.raises(VatReturnError, match="Ersten"):
        set_company_vat_method(
            session=session,
            company=company,
            method="ist",
            valid_from=date(2027, 1, 2),
            changed_by="t",
        )
    with pytest.raises(VatReturnError, match="bereits die Sollversteuerung"):
        set_company_vat_method(
            session=session,
            company=company,
            method="soll",
            valid_from=date(2027, 1, 1),
            changed_by="t",
        )
    with pytest.raises(VatReturnError, match="soll oder ist"):
        set_company_vat_method(
            session=session,
            company=company,
            method="anders",
            valid_from=date(2027, 1, 1),
            changed_by="t",
        )

    # Eine festgehaltene Voranmeldung bleibt und wird als Hinweis genannt.
    _invoice(session, company, accounts)
    save_vat_return(
        session=session,
        company_id=company.id,
        period_label="2027-01",
        changed_by="t",
    )
    hints = set_company_vat_method(
        session=session,
        company=company,
        method="ist",
        valid_from=date(2027, 1, 1),
        changed_by="t",
    )
    assert any("2027-01" in hint for hint in hints)
    assert company_vat_method(company, on=date(2027, 1, 1))["method"] == "ist"
    assert (
        session.execute(
            select(AuditLog).where(AuditLog.action == "vat_method_changed")
        ).scalar_one().company_id
        == company.id
    )

    # Zurück am selben Datum nimmt den Irrtum zurück.
    set_company_vat_method(
        session=session,
        company=company,
        method="soll",
        valid_from=date(2027, 1, 1),
        changed_by="t",
    )
    assert company.vat_cash_basis_from is None
    assert company.vat_cash_basis_until is None


def test_settlements_are_in_the_audit_export() -> None:
    assert EXPORT_TABLE_MODELS["open_item_settlements"] is OpenItemSettlement


def test_api_and_ui_cover_vat_method(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "API_REQUIRE_AUTH": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'ist.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db:
        company, _ = _seed(db)
        tenant = Tenant(name="Fremd")
        foreign = Company(tenant=tenant, name="Fremd GmbH", currency_code="EUR")
        db.add_all([tenant, foreign])
        db.flush()
        for username, role, token in (
            ("pruefer", "Pruefer", "obk_pruefer-ist"),
            ("buchhalter", "Buchhalter", "obk_buchhalter-ist"),
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
        company_id, foreign_id = company.id, foreign.id

    client = app.test_client()
    reader = {"Authorization": "Bearer obk_pruefer-ist"}
    writer = {"Authorization": "Bearer obk_buchhalter-ist"}
    url = f"/api/v1/companies/{company_id}/vat-method"
    assert client.get(url, headers=reader).get_json()["method"] == "soll"
    assert (
        client.post(
            url, headers=reader, json={"method": "ist", "valid_from": "2027-01-01"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            url, headers=writer, json={"method": "ist", "valid_from": "kaputt"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            url, headers=writer, json={"method": "ist", "valid_from": "2027-01-02"}
        ).status_code
        == 422
    )
    saved = client.post(url, headers=writer, json={"method": "ist", "valid_from": "2020-01-01"})
    assert saved.status_code == 200
    assert saved.get_json()["method"] == "ist"
    assert saved.get_json()["cash_basis_from"] == "2020-01-01"
    assert (
        client.get(f"/api/v1/companies/{foreign_id}/vat-method", headers=writer).status_code
        == 404
    )

    page_client = app.test_client()
    assert (
        page_client.post(
            "/auth/login", data={"username": "buchhalter", "password": "geheim123"}
        ).status_code
        == 302
    )
    page = page_client.get("/ustva", query_string={"company_id": company_id}).get_data(
        as_text=True
    )
    assert "Istversteuerung" in page


def test_migration_backfills_settlements_and_round_trips(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'ist-migration.db'}"
    session_factory = create_session_factory(url)
    with session_factory() as session:
        company, accounts = _seed(session)
        _, item = _invoice(session, company, accounts)
        payment = _payment(session, company, accounts, "19.00", date(2027, 2, 2))
        settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", amount=Decimal("100")
        )
        settle_open_item(
            session=session, open_item_id=item.id, changed_by="t", journal_entry_id=payment.id
        )
        item_id = item.id
    engine = create_engine(url)
    config = _alembic_config(engine)

    command.downgrade(config, "20261103_0060")
    assert "open_item_settlement" not in inspect(engine).get_table_names()
    assert "vat_cash_basis_from" not in {
        column["name"] for column in inspect(engine).get_columns("company")
    }
    with engine.connect() as connection:
        # Die Partner-Spalte der Vorgängerrevision bleibt, der Posten ebenfalls.
        assert connection.execute(
            text("SELECT status FROM open_item WHERE id = :id"), {"id": item_id}
        ).scalar_one() == "settled"

    command.upgrade(config, "head")
    with Session(engine) as session:
        rows = session.execute(
            select(OpenItemSettlement).where(OpenItemSettlement.open_item_id == item_id)
        ).scalars().all()
        assert sum(row.amount for row in rows) == Decimal("119.00")
        linked = [row for row in rows if row.journal_entry_id is not None]
        assert [row.amount for row in linked] == [Decimal("19.00")]
        assert linked[0].settlement_date == date(2027, 2, 2)
        assert session.get(OpenItem, item_id).status == "settled"
