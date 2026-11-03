"""Kassenbuch: Kassen (``cash_book``), unveränderbare Einträge (``cash_entry``) und
Tagesabschlüsse (``cash_closing``) samt Triggern gegen UPDATE/DELETE (ADR-007).

Revision ID: 20261103_0059
Revises: 20261103_0057
Create Date: 2026-11-03 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_0059"
down_revision = "20261103_0057"
branch_labels = None
depends_on = None

PROTECTED_TABLES = ("cash_entry", "cash_closing")
TRIGGER_OPERATIONS = (("UPDATE", "no_update"), ("DELETE", "no_delete"))


def _upgrade_sqlite_triggers() -> None:
    for table in PROTECTED_TABLES:
        for operation, suffix in TRIGGER_OPERATIONS:
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER obk_{table}_{suffix}
                    BEFORE {operation} ON {table}
                    BEGIN
                        SELECT RAISE(ABORT, '{table} rows are immutable');
                    END
                    """
                )
            )


def _upgrade_postgresql_triggers() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION obk_protect_cash_book_records()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
                    USING ERRCODE = 'integrity_constraint_violation';
            END;
            $$
            """
        )
    )
    for table in PROTECTED_TABLES:
        for operation, suffix in TRIGGER_OPERATIONS:
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER obk_{table}_{suffix}
                    BEFORE {operation} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION obk_protect_cash_book_records()
                    """
                )
            )


def _tenant_and_company_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "cash_book",
        *_tenant_and_company_columns(),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_entry_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "account_id", name="uq_cash_book_company_account"),
        sa.UniqueConstraint("company_id", "name", name="uq_cash_book_company_name"),
        sa.CheckConstraint("last_entry_number >= 0", name="ck_cash_book_entry_number"),
    )
    op.create_table(
        "cash_entry",
        *_tenant_and_company_columns(),
        sa.Column(
            "cash_book_id",
            sa.Integer(),
            sa.ForeignKey("cash_book.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("entry_number", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column(
            "contra_account_id", sa.Integer(), sa.ForeignKey("account.id", ondelete="RESTRICT")
        ),
        sa.Column("tax_code_id", sa.Integer(), sa.ForeignKey("tax_code.id", ondelete="RESTRICT")),
        sa.Column(
            "partner_id", sa.Integer(), sa.ForeignKey("business_partner.id", ondelete="RESTRICT")
        ),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("document.id", ondelete="RESTRICT")),
        sa.Column(
            "journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entry.id", ondelete="RESTRICT"),
        ),
        sa.Column("balance_after", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "reversal_of_id", sa.Integer(), sa.ForeignKey("cash_entry.id", ondelete="RESTRICT")
        ),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hash_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("cash_book_id", "entry_number", name="uq_cash_entry_book_number"),
        sa.UniqueConstraint("reversal_of_id", name="uq_cash_entry_reversal_of"),
        sa.CheckConstraint("entry_number > 0", name="ck_cash_entry_number_positive"),
        sa.CheckConstraint("amount > 0", name="ck_cash_entry_amount_positive"),
        sa.CheckConstraint("balance_after >= 0", name="ck_cash_entry_balance_non_negative"),
        sa.CheckConstraint(
            "entry_type IN ('receipt', 'payment', 'opening', 'difference', 'reversal')",
            name="ck_cash_entry_type",
        ),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_cash_entry_direction"),
        sa.CheckConstraint(
            "(entry_type = 'reversal' AND reversal_of_id IS NOT NULL) OR "
            "(entry_type <> 'reversal' AND reversal_of_id IS NULL)",
            name="ck_cash_entry_reversal_link",
        ),
        sa.CheckConstraint(
            "entry_type = 'opening' OR journal_entry_id IS NOT NULL",
            name="ck_cash_entry_journal_entry",
        ),
    )
    op.create_index("ix_cash_entry_book_date", "cash_entry", ["cash_book_id", "entry_date"])
    op.create_index("ix_cash_entry_journal_entry", "cash_entry", ["journal_entry_id"])
    op.create_table(
        "cash_closing",
        *_tenant_and_company_columns(),
        sa.Column(
            "cash_book_id",
            sa.Integer(),
            sa.ForeignKey("cash_book.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("closing_date", sa.Date(), nullable=False),
        sa.Column("expected_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("counted_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("difference", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "difference_entry_id",
            sa.Integer(),
            sa.ForeignKey("cash_entry.id", ondelete="RESTRICT"),
        ),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cash_book_id", "closing_date", name="uq_cash_closing_book_date"),
        sa.CheckConstraint("expected_balance >= 0", name="ck_cash_closing_expected"),
        sa.CheckConstraint("counted_balance >= 0", name="ck_cash_closing_counted"),
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _upgrade_sqlite_triggers()
    elif dialect == "postgresql":
        _upgrade_postgresql_triggers()
    else:
        raise RuntimeError(f"Unsupported database dialect for immutability triggers: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in PROTECTED_TABLES:
        for _operation, suffix in TRIGGER_OPERATIONS:
            trigger = f"obk_{table}_{suffix}"
            if dialect == "postgresql":
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON {table}"))
            else:
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))
    if dialect == "postgresql":
        op.execute(sa.text("DROP FUNCTION IF EXISTS obk_protect_cash_book_records()"))
    op.drop_table("cash_closing")
    op.drop_index("ix_cash_entry_journal_entry", table_name="cash_entry")
    op.drop_index("ix_cash_entry_book_date", table_name="cash_entry")
    op.drop_table("cash_entry")
    op.drop_table("cash_book")
