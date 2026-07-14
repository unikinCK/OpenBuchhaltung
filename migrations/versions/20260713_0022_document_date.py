"""Store the document date separately from the upload timestamp.

Revision ID: 20260713_0022
Revises: 20260713_0021
Create Date: 2026-07-13 21:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260713_0022"
down_revision = "20260713_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("document") as batch_op:
        batch_op.add_column(sa.Column("document_date", sa.Date(), nullable=True))
    op.create_index(
        "ix_document_company_date",
        "document",
        ["company_id", "document_date", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_company_date", table_name="document")
    with op.batch_alter_table("document") as batch_op:
        batch_op.drop_column("document_date")
