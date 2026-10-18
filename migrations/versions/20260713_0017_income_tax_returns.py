"""Ertragsteuern: KSt-/GewSt-Snapshots.

Revision ID: 20260713_0017
Revises: 20260713_0016
Create Date: 2026-07-13 01:10:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260713_0017"
down_revision = "20260713_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "income_tax_return",
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
        sa.Column("tax_type", sa.String(length=30), nullable=False),
        sa.Column("declaration_type", sa.String(length=30), nullable=False),
        sa.Column("period_label", sa.String(length=20), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="erstellt"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.UniqueConstraint(
            "company_id",
            "tax_type",
            "declaration_type",
            "period_label",
            name="uq_income_tax_return_scope",
        ),
        sa.CheckConstraint(
            "tax_type IN ('corporate_income', 'trade_tax')",
            name="ck_income_tax_return_tax_type_known",
        ),
        sa.CheckConstraint(
            "declaration_type IN ('declaration', 'prepayment_adjustment')",
            name="ck_income_tax_return_declaration_type_known",
        ),
        sa.CheckConstraint(
            "status IN ('erstellt', 'uebermittelt')",
            name="ck_income_tax_return_status_known",
        ),
        sa.CheckConstraint("date_from <= date_to", name="ck_income_tax_return_date_range"),
    )
    op.create_index(
        "ix_income_tax_return_company_period",
        "income_tax_return",
        ["company_id", "date_to"],
    )


def downgrade() -> None:
    op.drop_index("ix_income_tax_return_company_period", table_name="income_tax_return")
    op.drop_table("income_tax_return")
