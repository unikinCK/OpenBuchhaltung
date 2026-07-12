"""UStVA: vat_return-Tabelle (Kennziffern-Snapshot je Voranmeldungszeitraum).

Revision ID: 20260712_0011
Revises: 20260712_0010
Create Date: 2026-07-12 14:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0011"
down_revision = "20260712_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vat_return",
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
        sa.Column("period_label", sa.String(length=10), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("kennzahlen", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="erstellt"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.UniqueConstraint("company_id", "period_label", name="uq_vat_return_company_period"),
        sa.CheckConstraint("date_from <= date_to", name="ck_vat_return_date_range"),
        sa.CheckConstraint(
            "status IN ('erstellt', 'uebermittelt')", name="ck_vat_return_status_known"
        ),
    )


def downgrade() -> None:
    op.drop_table("vat_return")
