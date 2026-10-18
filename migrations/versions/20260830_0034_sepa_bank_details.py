"""SEPA-Zahllauf: Bankverbindung an Gesellschaft und offenen Posten.

Revision ID: 20260830_0034
Revises: 20260830_0033
Create Date: 2026-08-30 14:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0034"
down_revision = "20260830_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company", sa.Column("iban", sa.String(length=34), nullable=True))
    op.add_column("company", sa.Column("bic", sa.String(length=11), nullable=True))
    op.add_column(
        "open_item", sa.Column("counterparty_iban", sa.String(length=34), nullable=True)
    )
    op.add_column(
        "open_item", sa.Column("counterparty_bic", sa.String(length=11), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("open_item", "counterparty_bic")
    op.drop_column("open_item", "counterparty_iban")
    op.drop_column("company", "bic")
    op.drop_column("company", "iban")
