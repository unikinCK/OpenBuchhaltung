"""FinTS-Bankenliste (``fints_institute``): BLZ → PIN/TAN-URL und BIC.

Instanzweite Referenzdaten ohne Mandantenbezug; befüllt per Import der
Bankenliste der Deutschen Kreditwirtschaft (nicht mitgeliefert).

Revision ID: 20261011_0045
Revises: 20261009_0044
Create Date: 2026-10-11 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0045"
down_revision = "20261009_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fints_institute",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("blz", sa.String(length=8), nullable=False),
        sa.Column("bic", sa.String(length=11), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("data_center", sa.String(length=120), nullable=True),
        sa.Column("organisation", sa.String(length=60), nullable=True),
        sa.Column("fints_url", sa.String(length=255), nullable=True),
        sa.Column("fints_version", sa.String(length=40), nullable=True),
        sa.Column("last_changed", sa.Date(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fints_institute_blz", "fints_institute", ["blz"])


def downgrade() -> None:
    op.drop_index("ix_fints_institute_blz", table_name="fints_institute")
    op.drop_table("fints_institute")
