"""Leistungsdatum an Buchungen (``journal_entry.service_date``) und Hashversion 4.

Das Leistungsdatum bestimmt den Umsatzsteuer-Meldezeitraum (Sollversteuerung,
§ 18b/§ 18a Abs. 8 UStG). Neue Festschreibungen werden mit Hashversion 4 (inkl.
``service_date``) versiegelt. Bestehende Siegel der Versionen 2 und 3 bleiben
unverändert gültig; die DB-Wächter akzeptieren alle drei Versionen. Es findet
kein Re-Hashing statt.

Revision ID: 20261009_0044
Revises: 20261009_0043
Create Date: 2026-10-09 12:00:00
"""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20261009_0044"
down_revision = "20261009_0043"
branch_labels = None
depends_on = None

ACCEPTED_HASH_VERSIONS = (2, 3, 4)
PREVIOUS_HASH_VERSIONS = (2, 3)


def _canonical_timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_date(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


def _amount(value: Decimal | int | float | str) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):.2f}"


def _entry_hash_v3(entry: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    """Hashversion 3 (Stand Migration 0042) für den Downgrade von Version 4."""
    canonical = {
        "company_id": entry["company_id"],
        "content_hash_version": 3,
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
                "cost_center_id": line["cost_center_id"],
                "currency_code": line["currency_code"],
                "debit_amount": _amount(line["debit_amount"]),
                "description": line["description"],
                "id": line["id"],
                "line_number": line["line_number"],
                "partner_id": line["partner_id"],
                "profit_center_id": line["profit_center_id"],
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
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _hash_guard_condition(versions: tuple[int, ...]) -> str:
    accepted = ", ".join(str(version) for version in versions)
    return (
        "(NEW.is_finalized = 0 AND (NEW.content_hash IS NOT NULL "
        "OR NEW.content_hash_version IS NOT NULL)) OR "
        "(NEW.is_finalized = 1 AND (NEW.content_hash IS NULL "
        "OR length(NEW.content_hash) != 64 "
        "OR NEW.content_hash_version IS NULL "
        f"OR NEW.content_hash_version NOT IN ({accepted})))"
    )


def _replace_hash_guards(dialect: str, versions: tuple[int, ...]) -> None:
    """Ersetzt nur die Hash-Pflicht-Wächter; die Unveränderbarkeit bleibt aktiv."""
    if dialect == "sqlite":
        for operation in ("INSERT", "UPDATE"):
            trigger_name = f"obk_journal_entry_hash_required_on_{operation.lower()}"
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE {operation} ON journal_entry
                    WHEN {_hash_guard_condition(versions)}
                    BEGIN
                        SELECT RAISE(ABORT, 'finalized journal entry requires content hash');
                    END
                    """
                )
            )
    elif dialect == "postgresql":
        accepted = ", ".join(str(version) for version in versions)
        op.drop_constraint(
            "ck_journal_entry_finalized_content_hash",
            "journal_entry",
            type_="check",
        )
        op.create_check_constraint(
            "ck_journal_entry_finalized_content_hash",
            "journal_entry",
            "((is_finalized = false AND content_hash IS NULL "
            "AND content_hash_version IS NULL) OR "
            "(is_finalized = true AND content_hash IS NOT NULL "
            "AND length(content_hash) = 64 AND content_hash_version IS NOT NULL "
            f"AND content_hash_version IN ({accepted})))",
        )
    else:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")


def _drop_finalized_update_trigger(dialect: str) -> None:
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_update"))
    else:
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_update ON journal_entry"
            )
        )


def _restore_finalized_update_trigger(dialect: str) -> None:
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
    else:
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_journal_entry_finalized_no_update
                BEFORE UPDATE ON journal_entry
                FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry()
                """
            )
        )


def _downgrade_version_4_hashes(dialect: str) -> None:
    """Versiegelt Festschreibungen der Version 4 neu mit Version 3 (ohne Leistungsdatum)."""
    bind = op.get_bind()
    journal_entry = sa.table(
        "journal_entry",
        *[
            sa.column(name)
            for name in (
                "id",
                "tenant_id",
                "company_id",
                "fiscal_year_id",
                "period_id",
                "posting_number",
                "entry_date",
                "description",
                "source",
                "is_finalized",
                "finalized_at",
                "finalized_by",
                "reversal_of_id",
                "created_at",
                "content_hash_version",
                "content_hash",
            )
        ],
    )
    journal_line = sa.table(
        "journal_entry_line",
        *[
            sa.column(name)
            for name in (
                "id",
                "tenant_id",
                "journal_entry_id",
                "line_number",
                "account_id",
                "tax_code_id",
                "description",
                "debit_amount",
                "credit_amount",
                "currency_code",
                "cost_center_id",
                "profit_center_id",
                "partner_id",
            )
        ],
    )
    entries = bind.execute(
        sa.select(journal_entry)
        .where(
            journal_entry.c.is_finalized.is_(True),
            journal_entry.c.content_hash_version == 4,
        )
        .order_by(journal_entry.c.id)
    ).mappings()
    updates: list[tuple[int, str]] = []
    for result in entries:
        entry = dict(result)
        lines = [
            dict(line)
            for line in bind.execute(
                sa.select(journal_line)
                .where(journal_line.c.journal_entry_id == entry["id"])
                .order_by(journal_line.c.line_number, journal_line.c.id)
            ).mappings()
        ]
        updates.append((entry["id"], _entry_hash_v3(entry, lines)))
    if not updates:
        return
    _drop_finalized_update_trigger(dialect)
    for entry_id, content_hash in updates:
        bind.execute(
            sa.update(journal_entry)
            .where(journal_entry.c.id == entry_id)
            .values(content_hash_version=3, content_hash=content_hash)
        )
    _restore_finalized_update_trigger(dialect)


def upgrade() -> None:
    # Nullable Spalte ohne Tabellen-Rebuild: Die GoBD-Trigger bleiben erhalten.
    op.add_column("journal_entry", sa.Column("service_date", sa.Date(), nullable=True))
    op.create_index(
        "ix_journal_entry_company_service_date",
        "journal_entry",
        ["company_id", "service_date"],
    )
    _replace_hash_guards(op.get_bind().dialect.name, ACCEPTED_HASH_VERSIONS)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _downgrade_version_4_hashes(dialect)
    _replace_hash_guards(dialect, PREVIOUS_HASH_VERSIONS)
    op.drop_index("ix_journal_entry_company_service_date", table_name="journal_entry")
    if dialect == "sqlite":
        # ALTER TABLE … DROP COLUMN (SQLite ≥ 3.35) statt Rebuild: Trigger bleiben.
        # Festgeschriebene Buchungen sperrt der Update-Wächter nicht gegen DDL.
        op.execute(sa.text("ALTER TABLE journal_entry DROP COLUMN service_date"))
    else:
        op.drop_column("journal_entry", "service_date")
