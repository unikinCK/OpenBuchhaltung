"""Integration tests for database-level GoBD immutability guarantees."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_session_factory
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entry,
    reverse_journal_entry,
)
from domain.models import Account, AuditLog, Company, JournalEntry, JournalEntryLine, Tenant


@pytest.fixture()
def migrated_session(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'immutability.db'}"
    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        yield session


def _seed_entry(session: Session) -> tuple[Company, JournalEntry, int]:
    tenant = Tenant(name="Immutability Tenant")
    company = Company(tenant=tenant, name="Immutability GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    revenue = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="8400",
        name="Erlöse",
        account_type="revenue",
    )
    session.add_all([bank, revenue])
    session.commit()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 13),
            description="Unveränderbarkeitstest",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(account_id=bank.id, debit_amount=Decimal("100.00")),
                JournalLineInput(account_id=revenue.id, credit_amount=Decimal("100.00")),
            ],
        ),
    )
    return company, entry, bank.id


def _assert_integrity_error(
    session: Session,
    statement: str,
    parameters: dict[str, object],
    *,
    match: str,
) -> None:
    with pytest.raises(IntegrityError, match=match):
        try:
            session.execute(text(statement), parameters)
            session.commit()
        finally:
            session.rollback()


def test_finalized_entry_and_lines_are_immutable_in_database(
    migrated_session: Session,
) -> None:
    session = migrated_session
    company, entry, account_id = _seed_entry(session)

    # Open entries remain editable before the explicit finalization transition.
    session.execute(
        text("UPDATE journal_entry SET description = :description WHERE id = :id"),
        {"description": "Noch offen", "id": entry.id},
    )
    session.execute(
        text(
            "UPDATE journal_entry_line SET description = :description "
            "WHERE journal_entry_id = :entry_id AND line_number = 1"
        ),
        {"description": "Noch offen", "entry_id": entry.id},
    )
    session.commit()

    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="pytest")

    _assert_integrity_error(
        session,
        "UPDATE journal_entry SET description = :description WHERE id = :id",
        {"description": "Manipuliert", "id": entry.id},
        match="finalized journal entries are immutable",
    )
    _assert_integrity_error(
        session,
        "DELETE FROM journal_entry WHERE id = :id",
        {"id": entry.id},
        match="finalized journal entries are immutable",
    )
    _assert_integrity_error(
        session,
        "UPDATE journal_entry_line SET description = :description "
        "WHERE journal_entry_id = :entry_id AND line_number = 1",
        {"description": "Manipuliert", "entry_id": entry.id},
        match="lines of finalized journal entries are immutable",
    )
    _assert_integrity_error(
        session,
        "DELETE FROM journal_entry_line WHERE journal_entry_id = :entry_id AND line_number = 1",
        {"entry_id": entry.id},
        match="lines of finalized journal entries are immutable",
    )
    _assert_integrity_error(
        session,
        "INSERT INTO journal_entry_line "
        "(tenant_id, journal_entry_id, line_number, account_id, debit_amount, "
        "credit_amount, currency_code) "
        "VALUES (:tenant_id, :entry_id, 3, :account_id, 1, 0, 'EUR')",
        {
            "tenant_id": company.tenant_id,
            "entry_id": entry.id,
            "account_id": account_id,
        },
        match="lines of finalized journal entries are immutable",
    )

    assert session.scalar(select(JournalEntry.description).where(JournalEntry.id == entry.id)) == (
        "Noch offen"
    )
    assert (
        session.scalar(
            select(JournalEntryLine.description).where(
                JournalEntryLine.journal_entry_id == entry.id,
                JournalEntryLine.line_number == 1,
            )
        )
        == "Noch offen"
    )


def test_audit_log_is_append_only_in_database(migrated_session: Session) -> None:
    session = migrated_session
    _, entry, _ = _seed_entry(session)
    audit_id = session.scalar(
        select(AuditLog.id).where(
            AuditLog.entity_type == "journal_entry",
            AuditLog.entity_id == str(entry.id),
            AuditLog.action == "created",
        )
    )
    assert audit_id is not None

    _assert_integrity_error(
        session,
        "UPDATE audit_log SET action = :action WHERE id = :id",
        {"action": "manipulated", "id": audit_id},
        match="audit logs are append-only",
    )
    _assert_integrity_error(
        session,
        "DELETE FROM audit_log WHERE id = :id",
        {"id": audit_id},
        match="audit logs are append-only",
    )

    assert session.scalar(select(AuditLog.action).where(AuditLog.id == audit_id)) == "created"


def test_reversal_still_works_with_immutability_triggers(migrated_session: Session) -> None:
    session = migrated_session
    _, entry, _ = _seed_entry(session)
    finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="pytest")

    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 7, 13),
        changed_by="pytest",
    )

    assert reversal.reversal_of_id == entry.id
    assert reversal.is_finalized is True
    assert len(reversal.lines) == 2
