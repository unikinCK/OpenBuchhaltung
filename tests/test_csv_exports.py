from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from io import StringIO

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.services.journal_entries import JournalEntryInput, JournalLineInput, create_journal_entry
from app.services.reports import journal_entries_csv_for_company, trial_balance_csv_for_company
from domain.models import Account, Base, Company, Tenant


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_company_with_entry(session: Session) -> Company:
    tenant = Tenant(name="CSV Tenant")
    company = Company(name="CSV GmbH", currency_code="EUR", tenant=tenant)
    session.add_all([tenant, company])
    session.flush()

    account_debit = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    account_credit = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="8400",
        name="Erlöse",
        account_type="revenue",
    )
    session.add_all([account_debit, account_credit])
    session.commit()

    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 4),
            description="CSV Testbuchung",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(
                    account_id=account_debit.id,
                    debit_amount=Decimal("100.00"),
                    credit_amount=Decimal("0.00"),
                    description="Soll",
                ),
                JournalLineInput(
                    account_id=account_credit.id,
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("100.00"),
                    description="Haben",
                ),
            ],
        ),
    )

    return company


def test_trial_balance_csv_contains_header_and_values():
    with _build_session() as session:
        company = _seed_company_with_entry(session)

        csv_content = trial_balance_csv_for_company(session=session, company_id=company.id)
        rows = list(csv.DictReader(StringIO(csv_content)))

    assert rows[0]["code"] == "1200"
    assert rows[0]["debit_total"] == "100.00"
    assert rows[0]["credit_total"] == "0.00"
    assert rows[1]["code"] == "8400"
    assert rows[1]["balance"] == "-100.00"


def test_journal_csv_contains_header_and_values():
    with _build_session() as session:
        company = _seed_company_with_entry(session)

        csv_content = journal_entries_csv_for_company(session=session, company_id=company.id)
        rows = list(csv.DictReader(StringIO(csv_content)))

    assert len(rows) == 2
    assert rows[0]["posting_number"] == "2026-0001"
    assert rows[0]["entry_date"] == "2026-04-04"
    assert rows[0]["account_code"] == "1200"
    assert rows[0]["debit_amount"] == "100.00"
    assert rows[1]["account_code"] == "8400"
    assert rows[1]["credit_amount"] == "100.00"
