"""Bankseitige Referenz am Bankumsatz für die Duplikat-Erkennung.

Revision ID: 20260829_0030
Revises: 20260824_0029
Create Date: 2026-08-29 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260829_0030"
down_revision = "20260824_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bank_transaction", sa.Column("bank_reference", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("bank_transaction", "bank_reference")
