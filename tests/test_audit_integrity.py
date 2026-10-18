"""Integration tests for the tenant-scoped audit hash chain."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.db import _alembic_config
from app.services.audit_log import (
    GENESIS_HASH,
    log_audit_event,
    verify_audit_log_integrity,
)
from domain.models import AuditLog, Company, Tenant, User


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'audit_integrity.db'}",
        }
    )


def _seed_tenants(session: Session) -> tuple[Company, Company]:
    tenant_a = Tenant(name="Hash Tenant A")
    tenant_b = Tenant(name="Hash Tenant B")
    company_a = Company(tenant=tenant_a, name="Hash A GmbH", currency_code="EUR")
    company_b = Company(tenant=tenant_b, name="Hash B GmbH", currency_code="EUR")
    session.add_all([tenant_a, tenant_b, company_a, company_b])
    session.flush()
    return company_a, company_b


def test_audit_events_form_separate_valid_tenant_chains(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company_a, company_b = _seed_tenants(session)
        first = log_audit_event(
            session=session,
            tenant_id=company_a.tenant_id,
            company_id=company_a.id,
            entity_type="journal_entry",
            entity_id="1",
            action="created",
            changed_by="pytest",
            payload={"amount": "100.00", "note": "Gründung"},
        )
        second = log_audit_event(
            session=session,
            tenant_id=company_a.tenant_id,
            company_id=company_a.id,
            entity_type="journal_entry",
            entity_id="1",
            action="finalized",
            changed_by="pytest",
        )
        other_tenant = log_audit_event(
            session=session,
            tenant_id=company_b.tenant_id,
            company_id=company_b.id,
            entity_type="document",
            entity_id="7",
            action="uploaded",
            changed_by="pytest",
        )
        session.commit()

        assert first.sequence_number == 1
        assert first.previous_hash == GENESIS_HASH
        assert len(first.entry_hash) == 64
        assert second.sequence_number == 2
        assert second.previous_hash == first.entry_hash
        assert other_tenant.sequence_number == 1
        assert other_tenant.previous_hash == GENESIS_HASH

        result = verify_audit_log_integrity(session=session)
        assert result.valid is True
        assert result.checked_entries == 3
        assert result.checked_tenants == 2
        assert all(not tenant.issues for tenant in result.tenants)


def test_database_constraints_reject_a_fork_from_an_existing_predecessor(
    tmp_path: Path,
) -> None:
    app = _create_test_app(tmp_path)
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        company, _ = _seed_tenants(session)
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="journal_entry",
            entity_id="1",
            action="created",
            changed_by="pytest",
        )
        session.commit()
        tenant_id = company.tenant_id
        company_id = company.id

    with session_factory.kw["bind"].begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO audit_log "
                    "(tenant_id, company_id, entity_type, entity_id, action, "
                    "sequence_number, hash_version, previous_hash, entry_hash, "
                    "changed_by, changed_at) VALUES "
                    "(:tenant_id, :company_id, 'journal_entry', '2', 'created', "
                    "2, 1, :previous_hash, :entry_hash, 'attacker', :changed_at)"
                ),
                {
                    "tenant_id": tenant_id,
                    "company_id": company_id,
                    "previous_hash": GENESIS_HASH,
                    "entry_hash": "f" * 64,
                    "changed_at": datetime.now(timezone.utc),
                },
            )


def test_integrity_check_detects_privileged_content_tampering(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        company, _ = _seed_tenants(session)
        entry = log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="journal_entry",
            entity_id="1",
            action="created",
            changed_by="pytest",
            payload={"amount": "100.00"},
        )
        session.commit()
        entry_id = entry.id

    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        # Simulates a privileged attacker who first disables the append-only guard.
        connection.execute(text("DROP TRIGGER obk_audit_log_no_update"))
        connection.execute(
            text("UPDATE audit_log SET payload = :payload WHERE id = :id"),
            {"payload": '{"amount":"999.00"}', "id": entry_id},
        )

    with session_factory() as session:
        result = verify_audit_log_integrity(session=session)
    assert result.valid is False
    assert result.tenants[0].issues[0].code == "entry_hash_mismatch"

    runner_result = app.test_cli_runner().invoke(args=["verify-audit-log"])
    assert runner_result.exit_code == 1
    assert "entry_hash_mismatch" in runner_result.output


def test_integrity_check_detects_tail_truncation_against_tenant_anchor(
    tmp_path: Path,
) -> None:
    app = _create_test_app(tmp_path)
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        company, _ = _seed_tenants(session)
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="journal_entry",
            entity_id="1",
            action="created",
            changed_by="pytest",
        )
        last_entry = log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="journal_entry",
            entity_id="1",
            action="finalized",
            changed_by="pytest",
        )
        session.commit()
        last_entry_id = last_entry.id

    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER obk_audit_log_no_delete"))
        connection.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": last_entry_id})

    with session_factory() as session:
        result = verify_audit_log_integrity(session=session)
    assert result.valid is False
    assert {issue.code for issue in result.tenants[0].issues} == {
        "anchor_sequence_mismatch",
        "anchor_head_mismatch",
    }


def test_hash_chain_migration_backfills_existing_audit_entries(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_audit.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    config = _alembic_config(engine)
    command.upgrade(config, "20260713_0019")

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenant (id, name, created_at) "
                "VALUES (1, 'Legacy A', :now), (2, 'Legacy B', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO company "
                "(id, tenant_id, name, currency_code, fiscal_year_start_month, created_at) "
                "VALUES (1, 1, 'Legacy A GmbH', 'EUR', 1, :now), "
                "(2, 2, 'Legacy B GmbH', 'EUR', 1, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO audit_log "
                "(id, tenant_id, company_id, entity_type, entity_id, action, payload, "
                "changed_by, changed_at) VALUES "
                "(1, 1, 1, 'journal_entry', '1', 'created', :payload, 'legacy', :now), "
                "(2, 2, 2, 'document', '2', 'uploaded', NULL, 'legacy', :now), "
                "(3, 1, 1, 'journal_entry', '1', 'finalized', NULL, 'legacy', :now)"
            ),
            {"payload": '{"amount":"100.00"}', "now": now},
        )

    command.upgrade(config, "head")

    with Session(engine) as session:
        entries = (
            session.execute(select(AuditLog).order_by(AuditLog.tenant_id, AuditLog.sequence_number))
            .scalars()
            .all()
        )
        assert [(entry.tenant_id, entry.sequence_number) for entry in entries] == [
            (1, 1),
            (1, 2),
            (2, 1),
        ]
        assert entries[0].previous_hash == GENESIS_HASH
        assert entries[1].previous_hash == entries[0].entry_hash
        assert entries[2].previous_hash == GENESIS_HASH
        assert verify_audit_log_integrity(session=session).valid is True
        tenant_a = session.get(Tenant, 1)
        assert tenant_a.audit_sequence_number == 2
        assert tenant_a.audit_head_hash == entries[1].entry_hash

    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="audit logs are append-only"):
            connection.execute(text("UPDATE audit_log SET action = 'changed' WHERE id = 1"))


def test_integrity_is_exposed_via_api_ui_and_cli(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company, _ = _seed_tenants(session)
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="journal_entry",
            entity_id="1",
            action="created",
            changed_by="pytest",
        )
        session.add(
            User(
                username="audit-admin",
                password_hash=hash_password("audit-pass"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.commit()

    client = app.test_client()
    api_response = client.get("/api/v1/audit-log/integrity")
    assert api_response.status_code == 200
    assert api_response.get_json()["valid"] is True

    entries_response = client.get("/api/v1/audit-log")
    entry_payload = entries_response.get_json()["entries"][0]
    assert entry_payload["sequence_number"] == 1
    assert len(entry_payload["entry_hash"]) == 64

    client.post("/auth/login", data={"username": "audit-admin", "password": "audit-pass"})
    page_response = client.get("/audit-log?verify_integrity=1")
    assert page_response.status_code == 200
    assert "Integrität prüfen".encode() in page_response.data
    assert b"Intakt" in page_response.data

    compliance_response = client.get("/api/v1/integrity")
    assert compliance_response.status_code == 200
    assert compliance_response.get_json()["valid"] is True

    cli_result = app.test_cli_runner().invoke(args=["verify-audit-log"])
    assert cli_result.exit_code == 0
    assert "Audit-Hashkette intakt" in cli_result.output

    compliance_cli_result = app.test_cli_runner().invoke(args=["verify-integrity"])
    assert compliance_cli_result.exit_code == 0
    assert "Integritätsnachweise sind intakt" in compliance_cli_result.output
