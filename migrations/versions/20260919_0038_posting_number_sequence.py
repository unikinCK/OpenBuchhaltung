"""Nummernkreis je Geschäftsjahr für Buchungsnummern.

Revision ID: 20260919_0038
Revises: 20260919_0037
Create Date: 2026-09-19 11:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260919_0038"
down_revision = "20260919_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "posting_number_sequence",
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
        sa.Column(
            "fiscal_year_id",
            sa.Integer(),
            sa.ForeignKey("fiscal_year.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_number", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("fiscal_year_id", name="uq_posting_sequence_fiscal_year"),
        sa.CheckConstraint("last_number >= 0", name="ck_posting_sequence_non_negative"),
    )


def downgrade() -> None:
    op.drop_table("posting_number_sequence")
