"""Firmenwissen je Gesellschaft (ADR-004): Quellen, Quellenauszüge und Wissenseinträge
mit Status, Wiedervorlage und Herkunftspaket.

Revision ID: 20261031_0053
Revises: 20261027_0052
Create Date: 2026-10-31 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261031_0053"
down_revision = "20261027_0052"
branch_labels = None
depends_on = None


def _scope_columns() -> list[sa.Column]:
    return [
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
    ]


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "company_knowledge_source",
        *_scope_columns(),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("origin", sa.String(length=500), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("document_kind", sa.String(length=80), nullable=True),
        sa.Column("source_version", sa.String(length=80), nullable=True),
        sa.Column("source_date", sa.Date(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("authority", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("inactive_reason", sa.String(length=80), nullable=True),
        sa.Column("package_key", sa.String(length=120), nullable=True),
        sa.Column("package_revision", sa.String(length=80), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("company_id", "source_key", name="uq_company_knowledge_source_key"),
        sa.CheckConstraint(
            "authority IN ('binding', 'reference', 'historical', 'unverified')",
            name="ck_company_knowledge_source_authority",
        ),
    )
    op.create_table(
        "company_knowledge_excerpt",
        *_scope_columns(),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("company_knowledge_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("excerpt_key", sa.String(length=160), nullable=False),
        sa.Column("locator", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("package_key", sa.String(length=120), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "company_id", "excerpt_key", name="uq_company_knowledge_excerpt_key"
        ),
    )
    op.create_index(
        "ix_company_knowledge_excerpt_source", "company_knowledge_excerpt", ["source_id"]
    )
    op.create_table(
        "company_knowledge_entry",
        *_scope_columns(),
        sa.Column("entry_key", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("topic", sa.String(length=80), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("company_knowledge_source.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("locator", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("review_due", sa.Date(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("review_reason", sa.String(length=255), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("package_key", sa.String(length=120), nullable=True),
        sa.Column("proposed_by", sa.String(length=120), nullable=False),
        sa.Column("approved_by", sa.String(length=120), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("company_id", "entry_key", name="uq_company_knowledge_entry_key"),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'retired')",
            name="ck_company_knowledge_entry_status",
        ),
        sa.CheckConstraint(
            "kind IN ('knowledge', 'open_question')",
            name="ck_company_knowledge_entry_kind",
        ),
    )
    op.create_index(
        "ix_company_knowledge_entry_company_status",
        "company_knowledge_entry",
        ["company_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_knowledge_entry_company_status", table_name="company_knowledge_entry"
    )
    op.drop_table("company_knowledge_entry")
    op.drop_index("ix_company_knowledge_excerpt_source", table_name="company_knowledge_excerpt")
    op.drop_table("company_knowledge_excerpt")
    op.drop_table("company_knowledge_source")
