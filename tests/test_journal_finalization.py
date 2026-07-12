"""Tests für GoBD-Festschreibung und Storno von Journalbuchungen."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.services.datev_export import build_datev_export
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entries_until,
    finalize_journal_entry,
    reverse_journal_entry,
)
from app.services.periods import lock_period
from domain.models import (
    Account,
    AuditLog,
    Base,
    Company,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
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


def _seed_company(session: Session) -> Company:
    tenant = Tenant(name="GoBD Tenant")
    company = Company(tenant=tenant, name="GoBD GmbH", currency_code="EUR")
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
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="1776",
                name="Umsatzsteuer 19 %",
                account_type="liability",
            ),
        ]
    )
    session.commit()
    return company


def _account_id(session: Session, company: Company, code: str) -> int:
    return session.scalar(
        select(Account.id).where(Account.company_id == company.id, Account.code == code)
    )


def _create_entry(
    session: Session,
    company: Company,
    *,
    entry_date: date = date(2026, 5, 10),
    tax_code_id: int | None = None,
) -> JournalEntry:
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Testbuchung",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(
                    account_id=_account_id(session, company, "1200"),
                    debit_amount=Decimal("119.00"),
                ),
                JournalLineInput(
                    account_id=_account_id(session, company, "8400"),
                    credit_amount=Decimal("119.00") if tax_code_id is None else Decimal("100.00"),
                    tax_code_id=tax_code_id,
                ),
            ],
        ),
    )


def test_finalize_sets_flags_and_audit(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company)
    assert entry.is_finalized is False

    finalized = finalize_journal_entry(
        session=session, journal_entry_id=entry.id, changed_by="pytest"
    )

    assert finalized.is_finalized is True
    assert finalized.finalized_by == "pytest"
    assert finalized.finalized_at is not None

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "journal_entry",
            AuditLog.entity_id == str(entry.id),
            AuditLog.action == "finalized",
        )
    ).scalar_one()
    assert audit.payload["posting_number"] == entry.posting_number


def test_finalize_twice_fails(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company)
    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="pytest")

    with pytest.raises(JournalEntryCreationError, match="bereits festgeschrieben"):
        finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="pytest")


def test_bulk_finalize_until_date(session: Session) -> None:
    company = _seed_company(session)
    early = _create_entry(session, company, entry_date=date(2026, 3, 5))
    mid = _create_entry(session, company, entry_date=date(2026, 4, 30))
    late = _create_entry(session, company, entry_date=date(2026, 5, 1))

    count = finalize_journal_entries_until(
        session=session, company_id=company.id, up_to_date=date(2026, 4, 30), changed_by="pytest"
    )

    assert count == 2
    session.refresh(early)
    session.refresh(mid)
    session.refresh(late)
    assert early.is_finalized is True
    assert mid.is_finalized is True
    assert late.is_finalized is False

    # Zweiter Lauf findet nichts mehr.
    assert (
        finalize_journal_entries_until(
            session=session,
            company_id=company.id,
            up_to_date=date(2026, 4, 30),
            changed_by="pytest",
        )
        == 0
    )


def test_reverse_creates_mirrored_finalized_entry(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company)
    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="pytest")

    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 5, 15),
        changed_by="pytest",
    )

    assert reversal.reversal_of_id == entry.id
    assert reversal.source == "storno"
    assert reversal.is_finalized is True
    assert entry.posting_number in reversal.description

    original_lines = session.execute(
        select(JournalEntryLine)
        .where(JournalEntryLine.journal_entry_id == entry.id)
        .order_by(JournalEntryLine.line_number)
    ).scalars().all()
    reversal_lines = session.execute(
        select(JournalEntryLine)
        .where(JournalEntryLine.journal_entry_id == reversal.id)
        .order_by(JournalEntryLine.line_number)
    ).scalars().all()

    assert len(reversal_lines) == len(original_lines)
    for original_line, reversal_line in zip(original_lines, reversal_lines):
        assert reversal_line.account_id == original_line.account_id
        assert reversal_line.debit_amount == original_line.credit_amount
        assert reversal_line.credit_amount == original_line.debit_amount

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "journal_entry",
            AuditLog.entity_id == str(entry.id),
            AuditLog.action == "reversed",
        )
    ).scalar_one()
    assert audit.payload["reversal_posting_number"] == reversal.posting_number


def test_reverse_with_tax_lines_does_not_double_expand(session: Session) -> None:
    company = _seed_company(session)
    tax_code = TaxCode(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="USt19",
        rate=Decimal("19.00"),
        vat_account_id=_account_id(session, company, "1776"),
    )
    session.add(tax_code)
    session.commit()

    entry = _create_entry(session, company, tax_code_id=tax_code.id)
    original_line_count = len(
        session.execute(
            select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
        ).scalars().all()
    )
    assert original_line_count == 3  # Netto + Steuer + Bank

    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 5, 20),
        changed_by="pytest",
    )
    reversal_line_count = len(
        session.execute(
            select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == reversal.id)
        ).scalars().all()
    )
    assert reversal_line_count == original_line_count


def test_reverse_twice_fails(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company)
    reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 5, 15),
        changed_by="pytest",
    )

    with pytest.raises(JournalEntryCreationError, match="bereits storniert"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=entry.id,
            reversal_date=date(2026, 5, 16),
            changed_by="pytest",
        )


def test_reverse_of_reversal_fails(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company)
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 5, 15),
        changed_by="pytest",
    )

    with pytest.raises(JournalEntryCreationError, match="selbst eine Stornobuchung"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=reversal.id,
            reversal_date=date(2026, 5, 16),
            changed_by="pytest",
        )


def test_reverse_into_locked_period_fails(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company, entry_date=date(2026, 5, 10))
    # Periode der Originalbuchung sperren; Storno zum Originaldatum scheitert dann.
    lock_period(session=session, period_id=entry.period_id, locked_by="pytest")

    with pytest.raises(JournalEntryCreationError, match="gesperrt"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=entry.id,
            reversal_date=date(2026, 5, 10),
            changed_by="pytest",
        )

    # Zum Datum in einer offenen Periode klappt der Storno.
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 6, 1),
        changed_by="pytest",
    )
    assert reversal.entry_date == date(2026, 6, 1)


def test_datev_header_finalized_flag(session: Session) -> None:
    company = _seed_company(session)
    entry = _create_entry(session, company)

    generated_at = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    header = build_datev_export(
        session=session, company_id=company.id, generated_at=generated_at
    ).splitlines()[0]
    assert header.split(";")[20] == "0"

    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="pytest")
    header = build_datev_export(
        session=session, company_id=company.id, generated_at=generated_at
    ).splitlines()[0]
    assert header.split(";")[20] == "1"
