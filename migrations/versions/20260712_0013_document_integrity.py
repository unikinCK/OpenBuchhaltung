"""Belegintegritaet: Hash, Groesse und Versionierung.

Revision ID: 20260712_0013
Revises: 20260712_0012
Create Date: 2026-07-12 23:20:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0013"
down_revision = "20260712_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("document") as batch_op:
        batch_op.add_column(
            sa.Column("file_sha256", sa.String(length=64), nullable=False, server_default=""),
        )
        batch_op.add_column(
            sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        )
        batch_op.add_column(
            sa.Column("version_number", sa.Integer(), nullable=False, server_default="1"),
        )
        batch_op.add_column(
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        batch_op.add_column(sa.Column("replaces_document_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("replaced_at", sa.DateTime(timezone=True)))
        batch_op.create_foreign_key(
            "fk_document_replaces_document",
            "document",
            ["replaces_document_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_document_company_current",
        "document",
        ["company_id", "is_current", "uploaded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_company_current", table_name="document")
    with op.batch_alter_table("document") as batch_op:
        batch_op.drop_constraint("fk_document_replaces_document", type_="foreignkey")
        batch_op.drop_column("replaced_at")
        batch_op.drop_column("replaces_document_id")
        batch_op.drop_column("is_current")
        batch_op.drop_column("version_number")
        batch_op.drop_column("file_size_bytes")
        batch_op.drop_column("file_sha256")
