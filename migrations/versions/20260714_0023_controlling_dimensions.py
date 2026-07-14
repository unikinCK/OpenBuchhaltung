"""Add cost-center and profit-center dimensions to journal lines.

Revision ID: 20260714_0023
Revises: 20260713_0022
Create Date: 2026-07-14 10:00:00
"""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260714_0023"
down_revision = "20260713_0022"
branch_labels = None
depends_on = None


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


def _entry_hash(entry: dict[str, Any], lines: list[dict[str, Any]], version: int) -> str:
    canonical_lines = []
    for line in lines:
        canonical_line = {
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
        if version == 2:
            canonical_line["cost_center_id"] = line["cost_center_id"]
            canonical_line["profit_center_id"] = line["profit_center_id"]
        canonical_lines.append(canonical_line)
    canonical = {
        "company_id": entry["company_id"],
        "content_hash_version": version,
        "created_at": _canonical_timestamp(entry["created_at"]),
        "description": entry["description"],
        "entry_date": _canonical_date(entry["entry_date"]),
        "finalized_at": _canonical_timestamp(entry["finalized_at"]),
        "finalized_by": entry["finalized_by"],
        "fiscal_year_id": entry["fiscal_year_id"],
        "id": entry["id"],
        "lines": canonical_lines,
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


def _drop_journal_hash_guards(dialect: str) -> None:
    if dialect == "sqlite":
        for trigger_name in (
            "obk_journal_entry_finalized_no_update",
            "obk_journal_entry_hash_required_on_insert",
            "obk_journal_entry_hash_required_on_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS obk_journal_entry_finalized_no_update "
                "ON journal_entry"
            )
        )
        op.drop_constraint(
            "ck_journal_entry_finalized_content_hash",
            "journal_entry",
            type_="check",
        )
    else:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")


def _restore_journal_hash_guards(dialect: str, version: int) -> None:
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
        invalid = (
            "(NEW.is_finalized = 0 AND (NEW.content_hash IS NOT NULL "
            "OR NEW.content_hash_version IS NOT NULL)) OR "
            "(NEW.is_finalized = 1 AND (NEW.content_hash IS NULL "
            "OR length(NEW.content_hash) != 64 "
            "OR NEW.content_hash_version IS NULL "
            f"OR NEW.content_hash_version != {version}))"
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
        op.execute(
            sa.text(
                """
                CREATE TRIGGER obk_journal_entry_finalized_no_update
                BEFORE UPDATE ON journal_entry
                FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry()
                """
            )
        )
        op.create_check_constraint(
            "ck_journal_entry_finalized_content_hash",
            "journal_entry",
            "((is_finalized = false AND content_hash IS NULL "
            "AND content_hash_version IS NULL) OR "
            "(is_finalized = true AND content_hash IS NOT NULL "
            "AND length(content_hash) = 64 AND content_hash_version IS NOT NULL "
            f"AND content_hash_version = {version}))",
        )
    else:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")


def _rehash_finalized_entries(*, dialect: str, version: int) -> None:
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
            )
        ],
    )
    entries = bind.execute(
        sa.select(journal_entry)
        .where(journal_entry.c.is_finalized.is_(True))
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
        updates.append((entry["id"], _entry_hash(entry, lines, version)))
    _drop_journal_hash_guards(dialect)
    for entry_id, content_hash in updates:
        bind.execute(
            sa.update(journal_entry)
            .where(journal_entry.c.id == entry_id)
            .values(content_hash_version=version, content_hash=content_hash)
        )
    _restore_journal_hash_guards(dialect, version)


def _drop_sqlite_line_guards() -> None:
    for trigger_name in (
        "obk_journal_entry_line_finalized_no_insert",
        "obk_journal_entry_line_finalized_no_update",
        "obk_journal_entry_line_finalized_no_delete",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))


def _restore_sqlite_line_guards() -> None:
    statements = (
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_insert
        BEFORE INSERT ON journal_entry_line
        WHEN EXISTS (
            SELECT 1 FROM journal_entry
            WHERE id = NEW.journal_entry_id AND is_finalized = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_update
        BEFORE UPDATE ON journal_entry_line
        WHEN EXISTS (
            SELECT 1 FROM journal_entry
            WHERE id IN (OLD.journal_entry_id, NEW.journal_entry_id)
              AND is_finalized = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_delete
        BEFORE DELETE ON journal_entry_line
        WHEN EXISTS (
            SELECT 1 FROM journal_entry
            WHERE id = OLD.journal_entry_id AND is_finalized = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of finalized journal entries are immutable');
        END
        """,
    )
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    op.create_table(
        "controlling_unit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("unit_type", sa.String(length=30), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "unit_type IN ('cost_center', 'profit_center')",
            name="ck_controlling_unit_type",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name="ck_controlling_unit_validity",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["controlling_unit.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "company_id",
            "unit_type",
            "code",
            name="uq_controlling_unit_company_type_code",
        ),
    )
    op.create_index(
        "ix_controlling_unit_company_type_active",
        "controlling_unit",
        ["company_id", "unit_type", "is_active", "code"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        # Nullable REFERENCES-Spalten lassen sich ohne Tabellen-Rebuild ergänzen;
        # dadurch bleiben die GoBD-Trigger der Buchungszeilen erhalten.
        op.execute(
            sa.text(
                "ALTER TABLE journal_entry_line ADD COLUMN cost_center_id INTEGER "
                "REFERENCES controlling_unit(id) ON DELETE RESTRICT"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE journal_entry_line ADD COLUMN profit_center_id INTEGER "
                "REFERENCES controlling_unit(id) ON DELETE RESTRICT"
            )
        )
    else:
        op.add_column(
            "journal_entry_line",
            sa.Column(
                "cost_center_id",
                sa.Integer(),
                sa.ForeignKey("controlling_unit.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
        op.add_column(
            "journal_entry_line",
            sa.Column(
                "profit_center_id",
                sa.Integer(),
                sa.ForeignKey("controlling_unit.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
    op.create_index(
        "ix_journal_line_cost_center", "journal_entry_line", ["cost_center_id"]
    )
    op.create_index(
        "ix_journal_line_profit_center", "journal_entry_line", ["profit_center_id"]
    )
    _rehash_finalized_entries(dialect=dialect, version=2)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _rehash_finalized_entries(dialect=dialect, version=1)
    op.drop_index("ix_journal_line_profit_center", table_name="journal_entry_line")
    op.drop_index("ix_journal_line_cost_center", table_name="journal_entry_line")
    if dialect == "sqlite":
        _drop_sqlite_line_guards()
    with op.batch_alter_table("journal_entry_line") as batch_op:
        batch_op.drop_column("profit_center_id")
        batch_op.drop_column("cost_center_id")
    if dialect == "sqlite":
        _restore_sqlite_line_guards()
    op.drop_index(
        "ix_controlling_unit_company_type_active", table_name="controlling_unit"
    )
    op.drop_table("controlling_unit")
