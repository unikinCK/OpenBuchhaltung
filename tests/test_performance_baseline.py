from __future__ import annotations

from datetime import date
from decimal import Decimal
from time import perf_counter

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.bank_import import suggest_matches
from app.services.open_items import list_open_items
from app.services.reports import (
    balance_sheet_for_company,
    income_statement_for_company,
    trial_balance_for_company,
)
from domain.models import (
    Account,
    BankTransaction,
    Base,
    Company,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    Period,
    Tenant,
)

ENTRY_COUNT = 1_500
OPEN_ITEM_COUNT = 250
SMOKE_BUDGET_SECONDS = 6.0


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_performance_dataset(session: Session) -> dict[str, int]:
    tenant = Tenant(name="Performance Tenant")
    company = Company(tenant=tenant, name="Performance GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    fiscal_year = FiscalYear(
        tenant_id=tenant.id,
        company_id=company.id,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()

    periods = [
        Period(
            tenant_id=tenant.id,
            fiscal_year_id=fiscal_year.id,
            period_number=month,
            start_date=date(2026, month, 1),
            end_date=date(2026, month, 28),
            status="open",
        )
        for month in range(1, 13)
    ]
    session.add_all(periods)

    accounts = {
        "bank": Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        ),
        "cash": Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1000",
            name="Kasse",
            account_type="asset",
        ),
        "receivable": Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1400",
            name="Forderungen",
            account_type="receivable",
        ),
        "equity": Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="2000",
            name="Eigenkapital",
            account_type="equity",
        ),
        "expense": Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="4930",
            name="Bürobedarf",
            account_type="expense",
        ),
        "revenue": Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="8400",
            name="Erlöse",
            account_type="income",
        ),
    }
    session.add_all(accounts.values())
    session.flush()

    amount = Decimal("100.00")
    entries: list[JournalEntry] = []
    for idx in range(ENTRY_COUNT):
        month = idx % 12
        entry = JournalEntry(
            tenant_id=tenant.id,
            company_id=company.id,
            fiscal_year_id=fiscal_year.id,
            period_id=periods[month].id,
            posting_number=f"2026-{idx + 1:05d}",
            entry_date=date(2026, month + 1, (idx % 28) + 1),
            description=f"Performance Buchung {idx + 1}",
            source="performance-test",
        )
        if idx % 3 == 0:
            debit_account = accounts["bank"]
            credit_account = accounts["revenue"]
        elif idx % 3 == 1:
            debit_account = accounts["expense"]
            credit_account = accounts["bank"]
        else:
            debit_account = accounts["receivable"]
            credit_account = accounts["revenue"]
        entry.lines = [
            JournalEntryLine(
                tenant_id=tenant.id,
                line_number=1,
                account_id=debit_account.id,
                debit_amount=amount,
                currency_code="EUR",
            ),
            JournalEntryLine(
                tenant_id=tenant.id,
                line_number=2,
                account_id=credit_account.id,
                credit_amount=amount,
                currency_code="EUR",
            ),
        ]
        entries.append(entry)

    open_items = [
        OpenItem(
            tenant_id=tenant.id,
            company_id=company.id,
            account_id=accounts["receivable"].id,
            item_type="receivable",
            reference=f"RE-PERF-{idx + 1:04d}",
            counterparty=f"Kunde {idx + 1}",
            entry_date=date(2026, (idx % 12) + 1, 1),
            due_date=date(2026, (idx % 12) + 1, 28),
            original_amount=amount,
            open_amount=amount,
            currency_code="EUR",
            status="open",
        )
        for idx in range(OPEN_ITEM_COUNT)
    ]
    bank_transactions = [
        BankTransaction(
            tenant_id=tenant.id,
            company_id=company.id,
            bank_account_id=accounts["bank"].id,
            booking_date=date(2026, (idx % 12) + 1, 15),
            amount=amount if idx % 2 == 0 else -amount,
            currency_code="EUR",
            purpose=f"Performance Bankumsatz {idx + 1}",
            counterparty=f"Partner {idx + 1}",
            dedup_hash=f"perf-{idx + 1}",
            status="open",
        )
        for idx in range(300)
    ]

    session.add_all(entries)
    session.add_all(open_items)
    session.add_all(bank_transactions)
    session.commit()
    return {
        "company_id": company.id,
        "first_bank_transaction_id": bank_transactions[0].id,
    }


@pytest.mark.performance
def test_core_queries_have_ci_friendly_performance_baseline(session: Session) -> None:
    ids = _seed_performance_dataset(session)
    transaction = session.get(BankTransaction, ids["first_bank_transaction_id"])
    assert transaction is not None

    started = perf_counter()
    trial_balance = trial_balance_for_company(session=session, company_id=ids["company_id"])
    income_statement = income_statement_for_company(session=session, company_id=ids["company_id"])
    balance_sheet = balance_sheet_for_company(session=session, company_id=ids["company_id"])
    open_items = list_open_items(session=session, company_id=ids["company_id"])
    suggestions = suggest_matches(session=session, transaction=transaction)
    elapsed = perf_counter() - started

    assert len(trial_balance) >= 4
    assert income_statement["totals"]["total_revenue"] > Decimal("0.00")
    assert "is_balanced" in balance_sheet["totals"]
    assert len(open_items) == OPEN_ITEM_COUNT
    assert suggestions
    assert elapsed < SMOKE_BUDGET_SECONDS
