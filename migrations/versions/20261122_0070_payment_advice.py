"""Zahlungsavise: Auslesen, Zuordnung zu offenen Posten und Ausgleich.

Revision ID: 20261122_0070
Revises: 20261122_0069
Create Date: 2026-11-22

Ein Avis hängt an einem Beleg. Die KI-Abschrift je Seite und das Ergebnis der Zuordnung stehen als
JSON am Avis; der Ausgleich verweist auf Bankumsatz und Buchung.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261122_0070"
down_revision = "20261122_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_advice",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Integer(), sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id", sa.Integer(), sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id", sa.Integer(), sa.ForeignKey("document.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payer", sa.String(length=255), nullable=True),
        sa.Column("advice_number", sa.String(length=120), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=True),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("traffic_light", sa.String(length=10), nullable=True),
        sa.Column(
            "partner_id", sa.Integer(),
            sa.ForeignKey("business_partner.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("extraction", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column(
            "bank_transaction_id", sa.Integer(),
            sa.ForeignKey("bank_transaction.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "journal_entry_id", sa.Integer(),
            sa.ForeignKey("journal_entry.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", sa.String(length=120), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'ready', 'failed', 'applied')",
            name="ck_payment_advice_status",
        ),
        sa.CheckConstraint(
            "traffic_light IS NULL OR traffic_light IN ('green', 'yellow', 'red')",
            name="ck_payment_advice_traffic_light",
        ),
    )
    op.create_index("ix_payment_advice_company", "payment_advice", ["company_id", "id"])
    op.create_index("ix_payment_advice_document_id", "payment_advice", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_advice_document_id", table_name="payment_advice")
    op.drop_index("ix_payment_advice_company", table_name="payment_advice")
    op.drop_table("payment_advice")
