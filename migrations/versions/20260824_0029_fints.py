"""FinTS-Bankzugänge und eingefrorene TAN-Dialoge.

Revision ID: 20260824_0029
Revises: 20260823_0028
Create Date: 2026-08-23 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260824_0029"
down_revision = "20260823_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fints_connection",
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
        sa.Column("blz", sa.String(length=8), nullable=False),
        sa.Column("login", sa.String(length=120), nullable=False),
        sa.Column("fints_url", sa.String(length=255), nullable=False),
        sa.Column("sepa_iban", sa.String(length=34), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "name", name="uq_fints_connection_company_name"),
    )
    op.create_index(
        "ix_fints_connection_company", "fints_connection", ["company_id", "is_active"]
    )

    op.create_table(
        "fints_pending_dialog",
        sa.Column("id", sa.String(length=36), primary_key=True),
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
            "connection_id",
            sa.Integer(),
            sa.ForeignKey("fints_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.String(length=20), nullable=False),
        sa.Column("client_data", sa.LargeBinary(), nullable=False),
        sa.Column("dialog_data", sa.LargeBinary(), nullable=False),
        sa.Column("tan_request_data", sa.LargeBinary(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("to_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("fints_pending_dialog")
    op.drop_index("ix_fints_connection_company", table_name="fints_connection")
    op.drop_table("fints_connection")
