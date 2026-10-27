"""Belegabgleich im Hintergrund: Aufträge (``receipt_match_job``), damit der Abgleich
nicht mehr im Request läuft (Proxy-Timeout bei langsamen lokalen Modellen).

Revision ID: 20261027_0052
Revises: 20261018_0051
Create Date: 2026-10-27 11:30:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261027_0052"
down_revision = "20261018_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receipt_match_job",
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
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "suggestion_id",
            sa.Integer(),
            sa.ForeignKey("receipt_match_suggestion.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'failed')", name="ck_receipt_match_job_status"
        ),
    )
    op.create_index(
        "ix_receipt_match_job_document", "receipt_match_job", ["document_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_receipt_match_job_document", table_name="receipt_match_job")
    op.drop_table("receipt_match_job")
