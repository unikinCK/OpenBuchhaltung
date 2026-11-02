"""Unterlagen fürs Firmenwissen hochladen: Datei und Hintergrundauftrag
(``company_knowledge_upload``), damit Texterkennung und KI-Zusammenfassung nicht im
Request laufen.

Revision ID: 20261102_0056
Revises: 20261031_0055
Create Date: 2026-11-02 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261102_0056"
down_revision = "20261031_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_knowledge_upload",
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
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("source_fields", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("progress", sa.String(length=120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("company_knowledge_source.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "entry_id",
            sa.Integer(),
            sa.ForeignKey("company_knowledge_entry.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'failed')",
            name="ck_company_knowledge_upload_status",
        ),
    )
    op.create_index(
        "ix_company_knowledge_upload_file",
        "company_knowledge_upload",
        ["company_id", "file_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_company_knowledge_upload_file", table_name="company_knowledge_upload")
    op.drop_table("company_knowledge_upload")
