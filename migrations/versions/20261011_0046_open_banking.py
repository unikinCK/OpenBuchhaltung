"""Open-Banking-Bankzugänge (``open_banking_connection``) für Banken ohne FinTS.

PSD2-Abruf über Enable Banking: Bankauswahl, verschlüsselte Anbieter-Sitzung,
ausgewähltes Konto und Ablauf der Zustimmung.

Revision ID: 20261011_0046
Revises: 20261011_0045
Create Date: 2026-10-11 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0046"
down_revision = "20261011_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "open_banking_connection",
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
            "bank_account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("aspsp_name", sa.String(length=120), nullable=False),
        sa.Column("aspsp_country", sa.String(length=2), nullable=False),
        sa.Column("psu_type", sa.String(length=10), nullable=False),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("session_id_encrypted", sa.Text(), nullable=True),
        sa.Column("account_uid", sa.String(length=64), nullable=True),
        sa.Column("consent_valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_state", sa.String(length=64), nullable=True),
        sa.Column("auth_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "name", name="uq_open_banking_connection_company_name"),
        sa.UniqueConstraint("auth_state", name="uq_open_banking_connection_auth_state"),
    )


def downgrade() -> None:
    op.drop_table("open_banking_connection")
