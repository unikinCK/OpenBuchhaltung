"""Erfassungsweg von Belegen: ``document.capture_source``, ``captured_at`` und
``capture_device`` (mobile Belegerfassung der iOS-App).

Bestehende Belege bleiben ohne Angabe (Erfassungsweg unbekannt); es wird nichts
nachträglich erfunden.

Revision ID: 20261014_0049
Revises: 20261013_0048
Create Date: 2026-10-14 09:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261014_0049"
down_revision = "20261013_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document", sa.Column("capture_source", sa.String(length=30), nullable=True))
    op.add_column(
        "document", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("document", sa.Column("capture_device", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("document", "capture_device")
    op.drop_column("document", "captured_at")
    op.drop_column("document", "capture_source")
