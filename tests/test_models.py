from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from domain.models import (
    Account,
    Base,
    Company,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    Period,
    Tenant,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _create_base_entities(session: Session) -> tuple[Tenant, Company, FiscalYear, Period, Account]:
    tenant = Tenant(name="Test Tenant")
    company = Company(tenant=tenant, name="Test GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    fiscal_year = FiscalYear(
        tenant_id=tenant.id,
        company=company,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
    period = Period(
        tenant_id=tenant.id,
        fiscal_year=fiscal_year,
        period_number=1,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        status="open",
    )
    account = Account(
        tenant_id=tenant.id,
        company=company,
        code="1200",
        name="Bank",
        account_type="asset",
    )

    session.add_all([fiscal_year, period, account])
    session.commit()

    return tenant, company, fiscal_year, period, account


def test_can_create_central_entities(session: Session) -> None:
    tenant, company, fiscal_year, period, account = _create_base_entities(session)

    assert tenant.id is not None
    assert company.id is not None
    assert fiscal_year.id is not None
    assert period.id is not None
    assert account.id is not None


def test_foreign_key_constraints_are_enforced(session: Session) -> None:
    invalid_company = Company(tenant_id=9999, name="FK Fail GmbH", currency_code="EUR")
    session.add(invalid_company)

    with pytest.raises(IntegrityError):
        session.commit()


def test_journal_entry_line_integrity(session: Session) -> None:
    tenant, company, fiscal_year, period, account = _create_base_entities(session)

    entry = JournalEntry(
        tenant_id=tenant.id,
        company_id=company.id,
        fiscal_year_id=fiscal_year.id,
        period_id=period.id,
        posting_number="2026-0001",
        entry_date=date(2026, 1, 15),
        description="Musterbuchung",
        source="manual",
    )
    session.add(entry)
    session.flush()

    with pytest.raises(ValueError):
        JournalEntryLine(
            tenant_id=tenant.id,
            journal_entry_id=entry.id,
            line_number=1,
            account_id=account.id,
            debit_amount=Decimal("10.00"),
            credit_amount=Decimal("10.00"),
            currency_code="EUR",
        )
