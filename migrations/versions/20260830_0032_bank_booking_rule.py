"""Auto-Kontierungsregeln für Bankumsätze.

Revision ID: 20260830_0032
Revises: 20260829_0031
Create Date: 2026-08-30 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0032"
down_revision = "20260829_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_booking_rule",
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
        sa.Column("pattern", sa.String(length=120), nullable=False),
        sa.Column(
            "contra_account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tax_code_id",
            sa.Integer(),
            sa.ForeignKey("tax_code.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "cost_center_id",
            sa.Integer(),
            sa.ForeignKey("controlling_unit.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "profit_center_id",
            sa.Integer(),
            sa.ForeignKey("controlling_unit.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "pattern", name="uq_bank_rule_company_pattern"),
    )
    op.create_index("ix_bank_rule_company", "bank_booking_rule", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_bank_rule_company", table_name="bank_booking_rule")
    op.drop_table("bank_booking_rule")
