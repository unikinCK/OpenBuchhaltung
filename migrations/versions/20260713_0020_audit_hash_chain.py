"""Add a tenant-scoped cryptographic hash chain to the audit log.

Revision ID: 20260713_0020
Revises: 20260713_0019
Create Date: 2026-07-13 16:00:00
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260713_0020"
down_revision = "20260713_0019"
branch_labels = None
depends_on = None

AUDIT_HASH_VERSION = 1
GENESIS_HASH = "0" * 64


def _canonical_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decoded_payload(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _entry_hash(row: dict[str, Any], sequence_number: int, previous_hash: str) -> str:
    canonical = {
        "action": row["action"],
        "changed_at": _canonical_timestamp(row["changed_at"]),
        "changed_by": row["changed_by"],
        "company_id": row["company_id"],
        "entity_id": row["entity_id"],
        "entity_type": row["entity_type"],
        "hash_version": AUDIT_HASH_VERSION,
        "payload": _decoded_payload(row["payload"]),
        "previous_hash": previous_hash,
        "sequence_number": sequence_number,
        "tenant_id": row["tenant_id"],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audit_table() -> sa.TableClause:
    return sa.table(
        "audit_log",
        sa.column("id", sa.Integer()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("company_id", sa.Integer()),
        sa.column("entity_type", sa.String()),
        sa.column("entity_id", sa.String()),
        sa.column("action", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("changed_by", sa.String()),
        sa.column("changed_at", sa.DateTime(timezone=True)),
        sa.column("sequence_number", sa.Integer()),
        sa.column("hash_version", sa.Integer()),
        sa.column("previous_hash", sa.String()),
        sa.column("entry_hash", sa.String()),
    )


def _tenant_table() -> sa.TableClause:
    return sa.table(
        "tenant",
        sa.column("id", sa.Integer()),
        sa.column("audit_sequence_number", sa.Integer()),
        sa.column("audit_head_hash", sa.String()),
    )


def _drop_audit_protection(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_audit_log_no_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_audit_log_no_delete"))
    elif dialect == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_audit_log_no_update ON audit_log"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_audit_log_no_delete ON audit_log"))
    else:
        raise RuntimeError(f"Unsupported database dialect for audit hash chain: {dialect}")


def _create_audit_protection(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit logs are append-only');
                END
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit logs are append-only');
                END
                """
            )
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_audit_log_no_update
                BEFORE UPDATE ON audit_log
                FOR EACH ROW EXECUTE FUNCTION obk_protect_audit_log()
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_audit_log_no_delete
                BEFORE DELETE ON audit_log
                FOR EACH ROW EXECUTE FUNCTION obk_protect_audit_log()
                """
            )
        )
    else:
        raise RuntimeError(f"Unsupported database dialect for audit hash chain: {dialect}")


def _backfill_hash_chain() -> None:
    bind = op.get_bind()
    audit_log = _audit_table()
    tenant = _tenant_table()
    rows = (
        bind.execute(
            sa.select(
                audit_log.c.id,
                audit_log.c.tenant_id,
                audit_log.c.company_id,
                audit_log.c.entity_type,
                audit_log.c.entity_id,
                audit_log.c.action,
                audit_log.c.payload,
                audit_log.c.changed_by,
                audit_log.c.changed_at,
            ).order_by(audit_log.c.tenant_id, audit_log.c.id)
        )
        .mappings()
        .all()
    )

    current_tenant_id: int | None = None
    sequence_number = 0
    previous_hash = GENESIS_HASH
    for result in rows:
        row = dict(result)
        if row["tenant_id"] != current_tenant_id:
            current_tenant_id = row["tenant_id"]
            sequence_number = 0
            previous_hash = GENESIS_HASH
        sequence_number += 1
        entry_hash = _entry_hash(row, sequence_number, previous_hash)
        bind.execute(
            sa.update(audit_log)
            .where(audit_log.c.id == row["id"])
            .values(
                sequence_number=sequence_number,
                hash_version=AUDIT_HASH_VERSION,
                previous_hash=previous_hash,
                entry_hash=entry_hash,
            )
        )
        bind.execute(
            sa.update(tenant)
            .where(tenant.c.id == row["tenant_id"])
            .values(
                audit_sequence_number=sequence_number,
                audit_head_hash=entry_hash,
            )
        )
        previous_hash = entry_hash


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    _drop_audit_protection(dialect)

    with op.batch_alter_table("tenant") as batch_op:
        batch_op.add_column(
            sa.Column(
                "audit_sequence_number",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "audit_head_hash",
                sa.String(length=64),
                nullable=False,
                server_default=GENESIS_HASH,
            )
        )
        batch_op.create_check_constraint(
            "ck_tenant_audit_sequence_non_negative", "audit_sequence_number >= 0"
        )
        batch_op.create_check_constraint(
            "ck_tenant_audit_head_hash_length", "length(audit_head_hash) = 64"
        )

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.add_column(sa.Column("sequence_number", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "hash_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(sa.Column("previous_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("entry_hash", sa.String(length=64), nullable=True))

    _backfill_hash_chain()

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column("sequence_number", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("previous_hash", existing_type=sa.String(length=64), nullable=False)
        batch_op.alter_column("entry_hash", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_unique_constraint(
            "uq_audit_log_tenant_sequence", ["tenant_id", "sequence_number"]
        )
        batch_op.create_unique_constraint(
            "uq_audit_log_tenant_previous_hash", ["tenant_id", "previous_hash"]
        )
        batch_op.create_check_constraint("ck_audit_log_sequence_positive", "sequence_number > 0")
        batch_op.create_check_constraint("ck_audit_log_hash_version", "hash_version = 1")
        batch_op.create_check_constraint(
            "ck_audit_log_previous_hash_length", "length(previous_hash) = 64"
        )
        batch_op.create_check_constraint(
            "ck_audit_log_entry_hash_length", "length(entry_hash) = 64"
        )

    _create_audit_protection(dialect)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _drop_audit_protection(dialect)

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_constraint("ck_audit_log_entry_hash_length", type_="check")
        batch_op.drop_constraint("ck_audit_log_previous_hash_length", type_="check")
        batch_op.drop_constraint("ck_audit_log_hash_version", type_="check")
        batch_op.drop_constraint("ck_audit_log_sequence_positive", type_="check")
        batch_op.drop_constraint("uq_audit_log_tenant_previous_hash", type_="unique")
        batch_op.drop_constraint("uq_audit_log_tenant_sequence", type_="unique")
        batch_op.drop_column("entry_hash")
        batch_op.drop_column("previous_hash")
        batch_op.drop_column("hash_version")
        batch_op.drop_column("sequence_number")

    with op.batch_alter_table("tenant") as batch_op:
        batch_op.drop_constraint("ck_tenant_audit_head_hash_length", type_="check")
        batch_op.drop_constraint("ck_tenant_audit_sequence_non_negative", type_="check")
        batch_op.drop_column("audit_head_hash")
        batch_op.drop_column("audit_sequence_number")

    _create_audit_protection(dialect)
