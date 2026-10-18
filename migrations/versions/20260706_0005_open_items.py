"""Add open_item table.

Revision ID: 20260706_0005
Revises: 20260705_0004
Create Date: 2026-07-06 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260706_0005"
down_revision = "20260705_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "open_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("bank_transaction_id", sa.Integer(), nullable=True),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("reference", sa.String(length=120), nullable=False),
        sa.Column("counterparty", sa.String(length=255), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("original_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("open_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_by", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "original_amount > 0", name="ck_open_item_original_amount_positive"
        ),
        sa.CheckConstraint("open_amount >= 0", name="ck_open_item_open_amount_non_negative"),
        sa.CheckConstraint(
            "item_type IN ('receivable', 'payable')", name="ck_open_item_type_known"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'settled')", name="ck_open_item_status_known"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["bank_transaction_id"], ["bank_transaction.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("open_item")
