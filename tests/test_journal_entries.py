from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, AuditLog, Base, Company, FiscalYear, Period, PeriodLock, Tenant


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


def _seed_company_and_accounts(session: Session) -> Company:
    tenant = Tenant(name="Audit Tenant")
    company = Company(tenant=tenant, name="Audit GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    session.add_all(
        [
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="1200",
                name="Bank",
                account_type="asset",
            ),
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="8400",
                name="Erlöse",
                account_type="revenue",
            ),
        ]
    )
    session.commit()
    return company


def test_create_journal_entry_writes_audit_log(session: Session) -> None:
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 4),
            description="Audit-Test",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(
                    account_id=account_ids[0],
                    debit_amount=Decimal("100.00"),
                    credit_amount=Decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=account_ids[1],
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("100.00"),
                ),
            ],
        ),
    )

    audit_log = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "journal_entry",
            AuditLog.entity_id == str(entry.id),
            AuditLog.action == "created",
        )
    )
    assert audit_log is not None
    assert audit_log.changed_by == "pytest"
    assert audit_log.payload is not None
    assert audit_log.payload["posting_number"] == entry.posting_number


def test_create_journal_entry_rejects_locked_period(session: Session) -> None:
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()

    fiscal_year = FiscalYear(
        tenant_id=company.tenant_id,
        company_id=company.id,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()

    period = Period(
        tenant_id=company.tenant_id,
        fiscal_year_id=fiscal_year.id,
        period_number=4,
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        status="closed",
    )
    session.add(period)
    session.flush()

    session.add(
        PeriodLock(
            tenant_id=company.tenant_id,
            period_id=period.id,
            reason="Monatsabschluss",
            locked_by="pytest",
            locked_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    with pytest.raises(JournalEntryCreationError, match="Periode ist gesperrt"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 4, 10),
                description="Gesperrte-Periode-Test",
                status="posted",
                changed_by="pytest",
                lines=[
                    JournalLineInput(
                        account_id=account_ids[0],
                        debit_amount=Decimal("50.00"),
                        credit_amount=Decimal("0.00"),
                    ),
                    JournalLineInput(
                        account_id=account_ids[1],
                        debit_amount=Decimal("0.00"),
                        credit_amount=Decimal("50.00"),
                    ),
                ],
            ),
        )
