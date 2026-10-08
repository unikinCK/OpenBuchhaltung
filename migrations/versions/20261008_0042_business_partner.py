"""Geschäftspartner-Stammdaten, Sammelkonto-Kennzeichen und Partner an Buchungszeilen.

Neue Festschreibungen werden mit Hashversion 3 (inkl. ``partner_id`` je Zeile)
versiegelt. Bestehende Siegel der Version 2 bleiben unverändert gültig; die
DB-Wächter akzeptieren deshalb beide Versionen. Es findet kein Re-Hashing statt.

Revision ID: 20261008_0042
Revises: 20261008_0041
Create Date: 2026-10-08 14:00:00
"""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20261008_0042"
down_revision = "20261008_0041"
branch_labels = None
depends_on = None

ACCEPTED_HASH_VERSIONS = (2, 3)


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


def _entry_hash_v2(entry: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    """Hashversion 2 (Stand Migration 0023) für den Downgrade von Version 3."""
    canonical = {
        "company_id": entry["company_id"],
        "content_hash_version": 2,
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


def _downgrade_version_3_hashes(dialect: str) -> None:
    """Versiegelt Version-3-Buchungen für das alte Schema als Version 2 neu.

    Der Partnerbezug geht beim Downgrade ohnehin verloren (Spalte entfällt).
    """
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
        .where(
            journal_entry.c.is_finalized.is_(True),
            journal_entry.c.content_hash_version == 3,
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
        updates.append((entry["id"], _entry_hash_v2(entry, lines)))
    if not updates:
        return
    _drop_finalized_update_trigger(dialect)
    for entry_id, content_hash in updates:
        bind.execute(
            sa.update(journal_entry)
            .where(journal_entry.c.id == entry_id)
            .values(content_hash_version=2, content_hash=content_hash)
        )
    _restore_finalized_update_trigger(dialect)


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
        "business_partner",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("debtor_number", sa.String(length=20), nullable=True),
        sa.Column("creditor_number", sa.String(length=20), nullable=True),
        sa.Column(
            "partner_kind",
            sa.String(length=20),
            nullable=False,
            server_default="organization",
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("street", sa.String(length=255), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=False, server_default="DE"),
        sa.Column("vat_id", sa.String(length=20), nullable=True),
        sa.Column("tax_number", sa.String(length=30), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("contact_person", sa.String(length=255), nullable=True),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("bic", sa.String(length=11), nullable=True),
        sa.Column("payment_term_days", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "debtor_number IS NOT NULL OR creditor_number IS NOT NULL",
            name="ck_business_partner_role",
        ),
        sa.CheckConstraint(
            "payment_term_days IS NULL OR payment_term_days >= 0",
            name="ck_business_partner_payment_term",
        ),
        sa.CheckConstraint(
            "partner_kind IN ('organization', 'person')",
            name="ck_business_partner_kind",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "company_id", "debtor_number", name="uq_business_partner_company_debtor"
        ),
        sa.UniqueConstraint(
            "company_id", "creditor_number", name="uq_business_partner_company_creditor"
        ),
    )
    op.create_index(
        "ix_business_partner_company_active_name",
        "business_partner",
        ["company_id", "is_active", "name"],
    )

    # Sammelkonto-Kennzeichen; Bestandskonten bleiben unmarkiert und werden
    # bewusst über die Kontenpflege (mit Audit-Historie) gekennzeichnet.
    op.add_column("account", sa.Column("subledger", sa.String(length=10), nullable=True))

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        # Nullable REFERENCES-Spalte ohne Tabellen-Rebuild ergänzen; dadurch
        # bleiben die GoBD-Trigger der Buchungszeilen erhalten.
        op.execute(
            sa.text(
                "ALTER TABLE journal_entry_line ADD COLUMN partner_id INTEGER "
                "REFERENCES business_partner(id) ON DELETE RESTRICT"
            )
        )
    else:
        op.add_column(
            "journal_entry_line",
            sa.Column(
                "partner_id",
                sa.Integer(),
                sa.ForeignKey("business_partner.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
    op.create_index("ix_journal_line_partner", "journal_entry_line", ["partner_id"])
    _replace_hash_guards(dialect, ACCEPTED_HASH_VERSIONS)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    _downgrade_version_3_hashes(dialect)
    _replace_hash_guards(dialect, (2,))

    op.drop_index("ix_journal_line_partner", table_name="journal_entry_line")
    if dialect == "sqlite":
        _drop_sqlite_line_guards()
    with op.batch_alter_table("journal_entry_line") as batch_op:
        batch_op.drop_column("partner_id")
    if dialect == "sqlite":
        _restore_sqlite_line_guards()

    with op.batch_alter_table("account") as batch_op:
        batch_op.drop_column("subledger")

    op.drop_index("ix_business_partner_company_active_name", table_name="business_partner")
    op.drop_table("business_partner")
