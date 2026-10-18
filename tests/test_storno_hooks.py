"""Storno-Hooks (F4): Bankumsatz, AfA-Satz, Lohnlauf und OPOS folgen dem Storno."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.services.bank_import import book_transaction
from app.services.fixed_assets import (
    FixedAssetInput,
    create_fixed_asset,
    current_book_value,
    dispose_fixed_asset,
    post_depreciation,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.open_items import OpenItemInput, create_open_item, settle_open_item
from app.services.payroll import (
    PayrollEmployeeInput,
    PayrollRunInput,
    create_payroll_employee,
    create_payroll_run,
    post_payroll_run,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    DepreciationEntry,
    FixedAsset,
    JournalEntry,
    OpenItem,
    PayrollRun,
    Tenant,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


ACCOUNTS = {
    "1200": ("Bank", "asset"),
    "1400": ("Forderungen", "asset"),
    "0400": ("Maschinen", "asset"),
    "4200": ("Miete", "expense"),
    "4830": ("Abschreibungen", "expense"),
    "4120": ("Löhne und Gehälter", "expense"),
    "4130": ("Soziale Aufwendungen", "expense"),
    "1740": ("Verbindlichkeiten Lohn", "liability"),
    "1741": ("Verbindlichkeiten Lohnsteuer", "liability"),
    "1742": ("Verbindlichkeiten SV", "liability"),
    "8400": ("Erlöse", "income"),
}


def _seed(session: Session) -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name="Storno Tenant")
    company = Company(tenant=tenant, name="Storno GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts = {
        code: Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
        )
        for code, (name, account_type) in ACCOUNTS.items()
    }
    session.add_all(accounts.values())
    session.commit()
    return company, accounts


def _audit_actions(session: Session, entity_type: str) -> list[str]:
    return (
        session.execute(
            select(AuditLog.action).where(AuditLog.entity_type == entity_type).order_by(AuditLog.id)
        )
        .scalars()
        .all()
    )


def test_reversal_reopens_booked_bank_transaction(session: Session) -> None:
    company, accounts = _seed(session)
    transaction = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=accounts["1200"].id,
        booking_date=date(2026, 3, 3),
        amount=Decimal("-500.00"),
        currency_code="EUR",
        purpose="Miete März",
        dedup_hash="storno-bank-1",
    )
    session.add(transaction)
    session.commit()
    booked = book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["4200"].id,
        changed_by="pytest",
    )
    assert booked.status == "booked"
    first_entry_id = booked.journal_entry_id

    reverse_journal_entry(
        session=session,
        journal_entry_id=first_entry_id,
        reversal_date=date(2026, 3, 4),
        changed_by="pytest",
    )

    session.refresh(transaction)
    assert transaction.status == "open"
    assert transaction.journal_entry_id is None
    assert _audit_actions(session, "bank_transaction") == ["booked", "unbooked"]

    # Der Umsatz kann danach erneut (richtig) verbucht werden.
    rebooked = book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["4200"].id,
        changed_by="pytest",
    )
    assert rebooked.status == "booked"
    assert rebooked.journal_entry_id not in (None, first_entry_id)


def _asset(session: Session, company: Company) -> FixedAsset:
    return create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="A-001",
            name="Drehmaschine",
            acquisition_date=date(2026, 1, 1),
            acquisition_cost=Decimal("12000.00"),
            method="linear",
            useful_life_months=60,
            asset_account_code="0400",
            depreciation_account_code="4830",
            changed_by="pytest",
        ),
    )


def test_reversal_of_depreciation_restores_book_value(session: Session) -> None:
    company, _ = _seed(session)
    asset = _asset(session, company)
    entry = post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    assert current_book_value(session=session, asset=asset) == Decimal("9600.00")

    reverse_journal_entry(
        session=session,
        journal_entry_id=entry.journal_entry_id,
        reversal_date=date(2026, 12, 31),
        changed_by="pytest",
    )

    assert session.execute(select(DepreciationEntry.id)).first() is None
    assert current_book_value(session=session, asset=asset) == Decimal("12000.00")
    assert "depreciation_reversed" in _audit_actions(session, "fixed_asset")

    # Die AfA des Jahres kann erneut gebucht werden (Eindeutigkeit je Jahr ist frei).
    again = post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    assert again.amount == Decimal("2400.00")
    assert current_book_value(session=session, asset=asset) == Decimal("9600.00")


def test_reversal_of_disposal_reactivates_asset(session: Session) -> None:
    company, _ = _seed(session)
    asset = _asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    dispose_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        disposal_date=date(2027, 6, 30),
        proceeds=Decimal("5000.00"),
        changed_by="pytest",
    )
    disposal = session.execute(
        select(DepreciationEntry).where(DepreciationEntry.kind == "abgang")
    ).scalar_one()
    assert asset.status == "disposed"

    reverse_journal_entry(
        session=session,
        journal_entry_id=disposal.journal_entry_id,
        reversal_date=date(2027, 6, 30),
        changed_by="pytest",
    )

    session.refresh(asset)
    assert asset.status == "active"
    assert asset.disposal_date is None
    assert asset.disposal_proceeds is None
    # Nur die planmäßige AfA bleibt: Buchwert 12.000 − 2.400.
    assert current_book_value(session=session, asset=asset) == Decimal("9600.00")


def test_reversal_resets_payroll_run_to_draft(session: Session) -> None:
    company, _ = _seed(session)
    create_payroll_employee(
        session=session,
        payload=PayrollEmployeeInput(
            company_id=company.id,
            employee_number="P-001",
            first_name="Ada",
            last_name="Lovelace",
            employment_start=date(2026, 1, 1),
            gross_monthly_salary=Decimal("4000.00"),
            wage_tax_rate=Decimal("0.20"),
            employee_social_security_rate=Decimal("0.20"),
            employer_social_security_rate=Decimal("0.20"),
            wage_expense_account_code="4120",
            employer_social_security_expense_account_code="4130",
            payroll_liability_account_code="1740",
            wage_tax_liability_account_code="1741",
            social_security_liability_account_code="1742",
            changed_by="pytest",
        ),
    )
    run = create_payroll_run(
        session=session,
        payload=PayrollRunInput(
            company_id=company.id,
            period_label="2026-05",
            payment_date=date(2026, 5, 31),
            changed_by="pytest",
        ),
    )
    posted = post_payroll_run(session=session, payroll_run_id=run.id, changed_by="pytest")
    first_entry_id = posted.journal_entry_id

    reverse_journal_entry(
        session=session,
        journal_entry_id=first_entry_id,
        reversal_date=date(2026, 6, 1),
        changed_by="pytest",
    )

    run = session.get(PayrollRun, run.id)
    session.refresh(run)
    assert run.status == "draft"
    assert run.journal_entry_id is None
    assert run.posted_at is None
    assert _audit_actions(session, "payroll_run") == ["created", "posted", "unposted"]

    reposted = post_payroll_run(session=session, payroll_run_id=run.id, changed_by="pytest")
    assert reposted.status == "posted"
    assert reposted.journal_entry_id not in (None, first_entry_id)


def test_reversal_of_settlement_entry_reopens_open_item(session: Session) -> None:
    company, accounts = _seed(session)
    invoice = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 1),
            description="Ausgangsrechnung RE-1",
            status="posted",
            lines=[
                JournalLineInput(accounts["1400"].id, Decimal("1190.00"), Decimal("0.00")),
                JournalLineInput(accounts["8400"].id, Decimal("0.00"), Decimal("1190.00")),
            ],
        ),
    )
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=accounts["1400"].id,
            journal_entry_id=invoice.id,
            item_type="receivable",
            reference="RE-1",
            entry_date=date(2026, 7, 1),
            amount=Decimal("1190.00"),
            changed_by="pytest",
        ),
    )
    # Teilzahlung ohne Buchung, dann Restzahlung mit Ausgleichsbuchung.
    settle_open_item(
        session=session, open_item_id=item.id, amount=Decimal("190.00"), changed_by="pytest"
    )
    payment = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 10),
            description="Zahlungseingang RE-1",
            status="posted",
            lines=[
                JournalLineInput(accounts["1200"].id, Decimal("1000.00"), Decimal("0.00")),
                JournalLineInput(accounts["1400"].id, Decimal("0.00"), Decimal("1000.00")),
            ],
        ),
    )
    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        journal_entry_id=payment.id,
        changed_by="pytest",
    )
    assert settled.status == "settled"
    assert settled.settlement_journal_entry_id == payment.id
    assert settled.settlement_amount == Decimal("1000.00")

    reverse_journal_entry(
        session=session,
        journal_entry_id=payment.id,
        reversal_date=date(2026, 7, 11),
        changed_by="pytest",
    )

    item = session.get(OpenItem, item.id)
    session.refresh(item)
    assert item.status == "open"
    # Nur der stornierte Ausgleich lebt wieder auf, die Teilzahlung bleibt.
    assert item.open_amount == Decimal("1000.00")
    assert item.settlement_journal_entry_id is None
    assert item.settled_at is None
    assert _audit_actions(session, "open_item")[-1] == "reopened"

    # Das Storno der Rechnung selbst lässt den Posten unberührt.
    reverse_journal_entry(
        session=session,
        journal_entry_id=invoice.id,
        reversal_date=date(2026, 7, 12),
        changed_by="pytest",
    )
    session.refresh(item)
    assert item.status == "open"
    assert item.open_amount == Decimal("1000.00")


def test_reversal_audit_payload_lists_subledgers(session: Session) -> None:
    company, accounts = _seed(session)
    transaction = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=accounts["1200"].id,
        booking_date=date(2026, 3, 3),
        amount=Decimal("100.00"),
        currency_code="EUR",
        purpose="Erlös",
        dedup_hash="storno-bank-2",
    )
    session.add(transaction)
    session.commit()
    booked = book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=accounts["8400"].id,
        changed_by="pytest",
    )
    entry_id = booked.journal_entry_id
    reverse_journal_entry(
        session=session,
        journal_entry_id=entry_id,
        reversal_date=date(2026, 3, 4),
        changed_by="pytest",
    )
    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "journal_entry", AuditLog.action == "reversed"
        )
    ).scalar_one()
    assert audit.payload["subledgers"] == {"bank_transactions": [transaction.id]}
    assert session.get(JournalEntry, entry_id).reversed_by is not None
