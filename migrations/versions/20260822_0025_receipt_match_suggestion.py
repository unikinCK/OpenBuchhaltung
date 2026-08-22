"""Add receipt_match_suggestion table for the Belegabgleich workflow.

Revision ID: 20260822_0025
Revises: 20260714_0024
Create Date: 2026-08-22 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260822_0025"
down_revision = "20260714_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receipt_match_suggestion",
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
            "document_id",
            sa.Integer(),
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("suggestion_type", sa.String(length=20), nullable=False),
        sa.Column(
            "journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entry.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("llm_used", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("supplier", sa.String(length=120), nullable=True),
        sa.Column("invoice_number", sa.String(length=50), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("gross_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "suggestion_type IN ('match', 'new_booking')",
            name="ck_receipt_match_suggestion_type",
        ),
        sa.CheckConstraint(
            "status IN ('offen', 'freigegeben', 'abgelehnt')",
            name="ck_receipt_match_suggestion_status",
        ),
    )
    op.create_index(
        "ix_receipt_match_suggestion_company_status",
        "receipt_match_suggestion",
        ["company_id", "status"],
    )
    op.create_index(
        "ix_receipt_match_suggestion_document",
        "receipt_match_suggestion",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_receipt_match_suggestion_document", table_name="receipt_match_suggestion"
    )
    op.drop_index(
        "ix_receipt_match_suggestion_company_status", table_name="receipt_match_suggestion"
    )
    op.drop_table("receipt_match_suggestion")
