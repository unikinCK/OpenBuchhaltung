"""Mahnwesen: Mahnstufe und letztes Mahndatum am offenen Posten.

Revision ID: 20260830_0033
Revises: 20260830_0032
Create Date: 2026-08-30 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0033"
down_revision = "20260830_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("open_item") as batch:
        batch.add_column(
            sa.Column("dunning_level", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_dunning_date", sa.Date(), nullable=True))
        batch.create_check_constraint(
            "ck_open_item_dunning_level", "dunning_level BETWEEN 0 AND 3"
        )


def downgrade() -> None:
    with op.batch_alter_table("open_item") as batch:
        batch.drop_constraint("ck_open_item_dunning_level", type_="check")
        batch.drop_column("last_dunning_date")
        batch.drop_column("dunning_level")
