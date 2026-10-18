"""fixed_asset.degressive_rate auf Numeric(7, 4) erweitern.

Praxisübliche degressive AfA-Sätze haben mehr als 2 Nachkommastellen,
z. B. 23,0769 % (dreifacher linearer Satz bei 13 Jahren Nutzungsdauer,
3/13 × 100). Mit Numeric(5, 2) wurden sie auf 23,08 % gerundet, was über
lange Laufzeiten zu Abweichungen im AfA-Plan führt.

Revision ID: 20260823_0028
Revises: 20260822_0027
Create Date: 2026-08-23 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260823_0028"
down_revision = "20260822_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("fixed_asset") as batch:
        batch.alter_column(
            "degressive_rate",
            existing_type=sa.Numeric(5, 2),
            type_=sa.Numeric(7, 4),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Achtung: rundet vorhandene Sätze wieder auf 2 Nachkommastellen.
    with op.batch_alter_table("fixed_asset") as batch:
        batch.alter_column(
            "degressive_rate",
            existing_type=sa.Numeric(7, 4),
            type_=sa.Numeric(5, 2),
            existing_nullable=True,
        )
