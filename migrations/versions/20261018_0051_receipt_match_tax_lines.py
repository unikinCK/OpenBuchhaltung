"""Aufteilung je Steuersatz am Belegabgleich-Vorschlag (mehrere Steuersätze,
z. B. Restaurant 7 %/19 %), damit die Freigabe mehrzeilig buchen kann.

Bestehende Vorschläge bleiben ohne Aufteilung.

Revision ID: 20261018_0051
Revises: 20261018_0050
Create Date: 2026-10-18 23:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0051"
down_revision = "20261018_0050"
branch_labels = None
depends_on = None

TABLE = "receipt_match_suggestion"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("tax_lines", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column("tax_lines")
