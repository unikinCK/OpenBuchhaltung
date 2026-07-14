"""Integration tests for finalized booking and document integrity."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import create_app
from app.db import _alembic_config
from app.services.compliance_integrity import (
    calculate_journal_entry_content_hash,
    verify_compliance_integrity,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entry,
)
from domain.models import Account, Company, Document, JournalEntry, Tenant


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'compliance.db'}",
        }
    )


def _seed_entry(session: Session) -> tuple[Company, JournalEntry]:
    tenant = Tenant(name="Integrity Tenant")
    company = Company(tenant=tenant, name="Integrity GmbH", currency_code="EUR")
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
            description="Versiegelte Buchung",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(account_id=bank.id, debit_amount=Decimal("100.00")),
                JournalLineInput(account_id=revenue.id, credit_amount=Decimal("100.00")),
            ],
        ),
    )
    return company, entry


def test_finalization_seals_content_and_all_interfaces_verify_it(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company, entry = _seed_entry(session)
        assert entry.content_hash is None
        assert entry.content_hash_version is None

        finalized = finalize_journal_entry(
            session=session,
            journal_entry_id=entry.id,
            changed_by="pytest",
        )
        assert finalized.content_hash_version == 2
        assert len(finalized.content_hash or "") == 64
        assert finalized.content_hash == calculate_journal_entry_content_hash(finalized)

        result = verify_compliance_integrity(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
        )
        assert result.valid is True
        assert result.tenant_id == company.tenant_id
        assert result.company_id == company.id
        assert result.finalized_entries_checked == 1
        assert result.documents_checked == 0
        assert result.audit.checked_entries == 2

    client = app.test_client()
    response = client.get("/api/v1/integrity", query_string={"company_id": company.id})
    assert response.status_code == 200
    assert response.get_json()["valid"] is True
    assert response.get_json()["scope"] == {
        "tenant_id": company.tenant_id,
        "company_id": company.id,
        "audit_scope": "tenant",
    }

    journal_response = client.get(
        "/api/v1/journal-entries", query_string={"company_id": company.id}
    )
    payload = journal_response.get_json()["entries"][0]
    assert payload["content_hash_version"] == 2
    assert len(payload["content_hash"]) == 64

    cli_result = app.test_cli_runner().invoke(
        args=["verify-integrity", "--company-id", str(company.id)]
    )
    assert cli_result.exit_code == 0
    assert "1 festgeschriebene Buchungen" in cli_result.output


def test_integrity_detects_privileged_booking_tampering_and_missing_document(
    tmp_path: Path,
) -> None:
    app = _create_test_app(tmp_path)
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        company, entry = _seed_entry(session)
        finalize_journal_entry(
            session=session,
            journal_entry_id=entry.id,
            changed_by="pytest",
        )
        session.add(
            Document(
                tenant_id=company.tenant_id,
                company_id=company.id,
                journal_entry_id=entry.id,
                file_name="missing.pdf",
                storage_key=str(tmp_path / "missing.pdf"),
                mime_type="application/pdf",
                file_sha256="0" * 64,
                file_size_bytes=10,
            )
        )
        session.commit()
        entry_id = entry.id
        tenant_id = company.tenant_id
        company_id = company.id

    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER obk_journal_entry_finalized_no_update"))
        connection.execute(
            text("UPDATE journal_entry SET description = 'Manipuliert' WHERE id = :id"),
            {"id": entry_id},
        )

    with session_factory() as session:
        result = verify_compliance_integrity(
            session=session,
            tenant_id=tenant_id,
            company_id=company_id,
        )
    assert result.valid is False
    assert {(issue.area, issue.code) for issue in result.issues} == {
        ("journal_entry", "content_hash_mismatch"),
        ("document", "file_missing"),
    }

    cli_result = app.test_cli_runner().invoke(
        args=["verify-integrity", "--company-id", str(company_id)]
    )
    assert cli_result.exit_code == 1
    assert "content_hash_mismatch" in cli_result.output
    assert "file_missing" in cli_result.output


def test_database_requires_a_hash_for_finalization(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        _, entry = _seed_entry(session)
        with pytest.raises(IntegrityError, match="requires content hash"):
            session.execute(
                text(
                    "UPDATE journal_entry SET is_finalized = 1, "
                    "finalized_at = :now, finalized_by = 'bypass' WHERE id = :id"
                ),
                {"now": datetime.now(timezone.utc), "id": entry.id},
            )
            session.commit()


def test_migration_backfills_existing_finalized_journal_hashes(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_journal.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    config = _alembic_config(engine)
    command.upgrade(config, "20260713_0020")

    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenant (id, name, created_at) VALUES (1, 'Legacy', :now)"),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO company "
                "(id, tenant_id, name, currency_code, fiscal_year_start_month, created_at) "
                "VALUES (1, 1, 'Legacy GmbH', 'EUR', 1, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO fiscal_year "
                "(id, tenant_id, company_id, label, start_date, end_date, is_closed) "
                "VALUES (1, 1, 1, '2026', '2026-01-01', '2026-12-31', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO period "
                "(id, tenant_id, fiscal_year_id, period_number, start_date, end_date, "
                "status, is_closing) "
                "VALUES (1, 1, 1, 7, '2026-07-01', '2026-07-31', 'open', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO account "
                "(id, tenant_id, company_id, code, name, account_type, hierarchy_level, "
                "level_1, level_2, level_3, level_4, is_active) "
                "VALUES (1, 1, 1, '1200', 'Bank', 'asset', 2, '1', '2', '0', '0', 1), "
                "(2, 1, 1, '8400', 'Erlöse', 'revenue', 2, '8', '4', '0', '0', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO journal_entry "
                "(id, tenant_id, company_id, fiscal_year_id, period_id, posting_number, "
                "entry_date, description, source, is_finalized, finalized_at, finalized_by, "
                "created_at) VALUES "
                "(1, 1, 1, 1, 1, '2026-0001', '2026-07-13', 'Legacy Buchung', "
                "'manual', 0, NULL, NULL, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO journal_entry_line "
                "(id, tenant_id, journal_entry_id, line_number, account_id, debit_amount, "
                "credit_amount, currency_code) VALUES "
                "(1, 1, 1, 1, 1, 100, 0, 'EUR'), "
                "(2, 1, 1, 2, 2, 0, 100, 'EUR')"
            )
        )
        connection.execute(
            text(
                "UPDATE journal_entry SET is_finalized = 1, finalized_at = :now, "
                "finalized_by = 'legacy' WHERE id = 1"
            ),
            {"now": now},
        )

    command.upgrade(config, "head")

    with Session(engine) as session:
        entry = session.scalar(select(JournalEntry).where(JournalEntry.id == 1))
        assert entry is not None
        assert entry.content_hash_version == 2
        assert len(entry.content_hash or "") == 64
        assert entry.content_hash == calculate_journal_entry_content_hash(entry)
        assert verify_compliance_integrity(session=session, company_id=1).valid is True

    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="finalized journal entries are immutable"):
            connection.execute(
                text("UPDATE journal_entry SET description = 'changed' WHERE id = 1")
            )
