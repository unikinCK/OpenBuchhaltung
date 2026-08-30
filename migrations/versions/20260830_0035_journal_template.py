"""Buchungsvorlagen für wiederkehrende Buchungen.

Revision ID: 20260830_0035
Revises: 20260830_0034
Create Date: 2026-08-30 16:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0035"
down_revision = "20260830_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journal_template",
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
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("interval", sa.String(length=20), nullable=False),
        sa.Column("next_run", sa.Date(), nullable=True),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "name", name="uq_journal_template_company_name"),
        sa.CheckConstraint(
            "interval IN ('on_demand', 'monthly', 'quarterly', 'yearly')",
            name="ck_journal_template_interval",
        ),
    )
    op.create_index("ix_journal_template_company", "journal_template", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_journal_template_company", table_name="journal_template")
    op.drop_table("journal_template")
