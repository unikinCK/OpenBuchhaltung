"""Seal finalized journal entries with a reproducible SHA-256 content hash.

Revision ID: 20260713_0021
Revises: 20260713_0020
Create Date: 2026-07-13 18:00:00
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260713_0021"
down_revision = "20260713_0020"
branch_labels = None
depends_on = None

JOURNAL_CONTENT_HASH_VERSION = 1


def _canonical_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


def _amount(value: Decimal | int | float | str) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):.2f}"


def _entry_hash(entry: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    canonical = {
        "company_id": entry["company_id"],
        "content_hash_version": JOURNAL_CONTENT_HASH_VERSION,
        "created_at": _canonical_timestamp(entry["created_at"]),
        "description": entry["description"],
        "entry_date": _canonical_date(entry["entry_date"]),
        "finalized_at": _canonical_timestamp(entry["finalized_at"]),
        "finalized_by": entry["finalized_by"],
        "fiscal_year_id": entry["fiscal_year_id"],
        "id": entry["id"],
        "lines": [
            {
                "account_id": line["account_id"],
                "credit_amount": _amount(line["credit_amount"]),
                "currency_code": line["currency_code"],
                "debit_amount": _amount(line["debit_amount"]),
                "description": line["description"],
                "id": line["id"],
                "line_number": line["line_number"],
                "tax_code_id": line["tax_code_id"],
                "tenant_id": line["tenant_id"],
            }
            for line in lines
        ],
        "period_id": entry["period_id"],
        "posting_number": entry["posting_number"],
        "reversal_of_id": entry["reversal_of_id"],
        "source": entry["source"],
        "tenant_id": entry["tenant_id"],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _journal_entry_table() -> sa.TableClause:
    return sa.table(
        "journal_entry",
        sa.column("id", sa.Integer()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("company_id", sa.Integer()),
        sa.column("fiscal_year_id", sa.Integer()),
        sa.column("period_id", sa.Integer()),
        sa.column("posting_number", sa.String()),
        sa.column("entry_date", sa.Date()),
        sa.column("description", sa.Text()),
        sa.column("source", sa.String()),
        sa.column("is_finalized", sa.Boolean()),
        sa.column("finalized_at", sa.DateTime(timezone=True)),
        sa.column("finalized_by", sa.String()),
        sa.column("reversal_of_id", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("content_hash_version", sa.Integer()),
        sa.column("content_hash", sa.String()),
    )


def _journal_line_table() -> sa.TableClause:
    return sa.table(
        "journal_entry_line",
        sa.column("id", sa.Integer()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("journal_entry_id", sa.Integer()),
        sa.column("line_number", sa.Integer()),
        sa.column("account_id", sa.Integer()),
        sa.column("tax_code_id", sa.Integer()),
        sa.column("description", sa.String()),
        sa.column("debit_amount", sa.Numeric(14, 2)),
        sa.column("credit_amount", sa.Numeric(14, 2)),
        sa.column("currency_code", sa.String()),
    )


def _drop_immutable_update_trigger(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_update"))
    elif dialect == "postgresql":
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_update ON journal_entry")
        )
    else:
        raise RuntimeError(f"Unsupported database dialect for journal hashes: {dialect}")


def _create_immutable_update_trigger(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_journal_entry_finalized_no_update
                BEFORE UPDATE ON journal_entry
                WHEN OLD.is_finalized = 1
                BEGIN
                    SELECT RAISE(ABORT, 'finalized journal entries are immutable');
                END
                """
            )
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_journal_entry_finalized_no_update
                BEFORE UPDATE ON journal_entry
                FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry()
                """
            )
        )
    else:
        raise RuntimeError(f"Unsupported database dialect for journal hashes: {dialect}")


def _backfill_finalized_hashes(dialect: str) -> None:
    bind = op.get_bind()
    journal_entry = _journal_entry_table()
    journal_line = _journal_line_table()
    entries = (
        bind.execute(
            sa.select(journal_entry)
            .where(journal_entry.c.is_finalized.is_(True))
            .order_by(journal_entry.c.id)
        )
        .mappings()
        .all()
    )
    updates: list[tuple[int, str]] = []
    for result in entries:
        entry = dict(result)
        if entry["finalized_at"] is None or not entry["finalized_by"]:
            raise RuntimeError(
                "Cannot seal finalized journal entry "
                f"{entry['id']}: finalization metadata is incomplete."
            )
        lines = [
            dict(line)
            for line in bind.execute(
                sa.select(journal_line)
                .where(journal_line.c.journal_entry_id == entry["id"])
                .order_by(journal_line.c.line_number, journal_line.c.id)
            ).mappings()
        ]
        updates.append((entry["id"], _entry_hash(entry, lines)))

    _drop_immutable_update_trigger(dialect)
    for entry_id, content_hash in updates:
        bind.execute(
            sa.update(journal_entry)
            .where(journal_entry.c.id == entry_id)
            .values(
                content_hash_version=JOURNAL_CONTENT_HASH_VERSION,
                content_hash=content_hash,
            )
        )
    _create_immutable_update_trigger(dialect)


def _create_hash_validation(dialect: str) -> None:
    condition = (
        "((is_finalized = false AND content_hash IS NULL "
        "AND content_hash_version IS NULL) OR "
        "(is_finalized = true AND content_hash IS NOT NULL "
        "AND length(content_hash) = 64 AND content_hash_version IS NOT NULL "
        "AND content_hash_version = 1))"
    )
    if dialect == "sqlite":
        invalid = (
            "(NEW.is_finalized = 0 AND (NEW.content_hash IS NOT NULL "
            "OR NEW.content_hash_version IS NOT NULL)) OR "
            "(NEW.is_finalized = 1 AND (NEW.content_hash IS NULL "
            "OR length(NEW.content_hash) != 64 "
            "OR NEW.content_hash_version IS NULL "
            "OR NEW.content_hash_version != 1))"
        )
        for operation in ("INSERT", "UPDATE"):
            trigger_name = f"obk_journal_entry_hash_required_on_{operation.lower()}"
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE {operation} ON journal_entry
                    WHEN {invalid}
                    BEGIN
                        SELECT RAISE(ABORT, 'finalized journal entry requires content hash');
                    END
                    """
                )
            )
    elif dialect == "postgresql":
        op.create_check_constraint(
            "ck_journal_entry_finalized_content_hash",
            "journal_entry",
            condition,
        )
    else:
        raise RuntimeError(f"Unsupported database dialect for journal hashes: {dialect}")


def _drop_hash_validation(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_hash_required_on_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_hash_required_on_insert"))
    elif dialect == "postgresql":
        op.drop_constraint(
            "ck_journal_entry_finalized_content_hash",
            "journal_entry",
            type_="check",
        )
    else:
        raise RuntimeError(f"Unsupported database dialect for journal hashes: {dialect}")


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    with op.batch_alter_table("journal_entry") as batch_op:
        batch_op.add_column(sa.Column("content_hash_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))

    _backfill_finalized_hashes(dialect)
    _create_hash_validation(dialect)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _drop_hash_validation(dialect)
    if dialect == "sqlite":
        # SQLite rebuilds the table while dropping columns, which also removes
        # triggers attached directly to journal_entry. Restore the guards from
        # revision 0019 after the rebuild.
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_delete"))
    with op.batch_alter_table("journal_entry") as batch_op:
        batch_op.drop_column("content_hash")
        batch_op.drop_column("content_hash_version")
    if dialect == "sqlite":
        _create_immutable_update_trigger(dialect)
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_journal_entry_finalized_no_delete
                BEFORE DELETE ON journal_entry
                WHEN OLD.is_finalized = 1
                BEGIN
                    SELECT RAISE(ABORT, 'finalized journal entries are immutable');
                END
                """
            )
        )
