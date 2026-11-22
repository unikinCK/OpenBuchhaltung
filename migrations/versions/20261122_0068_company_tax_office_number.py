"""Bundesfinanzamtsnummer an der Gesellschaft für den ELSTER-Datensatz.

Revision ID: 20261122_0068
Revises: 20261122_0067
Create Date: 2026-11-22

Die ELSTER-Steuernummer (13 Stellen) besteht aus der vierstelligen Bundesfinanzamtsnummer, einer 0
und den letzten acht Ziffern der Steuernummer laut Bescheid. Die Bundesfinanzamtsnummer ist zugleich
Empfänger der Übermittlung (ADR-011).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261122_0068"
down_revision = "20261122_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("company", sa.Column("tax_office_number", sa.String(length=4), nullable=True))


def downgrade() -> None:
    op.drop_column("company", "tax_office_number")
