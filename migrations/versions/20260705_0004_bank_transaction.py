"""Add bank_transaction table.

Revision ID: 20260705_0004
Revises: 20260705_0003
Create Date: 2026-07-05 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260705_0004"
down_revision = "20260705_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_transaction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("purpose", sa.String(length=255), nullable=False),
        sa.Column("counterparty", sa.String(length=255), nullable=True),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bank_account_id"], ["account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "dedup_hash", name="uq_bank_tx_company_hash"),
        sa.CheckConstraint("amount != 0", name="ck_bank_tx_amount_non_zero"),
    )


def downgrade() -> None:
    op.drop_table("bank_transaction")
