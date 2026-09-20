"""Ausgleichsbuchung am offenen Posten (für Storno-Hooks).

Revision ID: 20260919_0039
Revises: 20260919_0038
Create Date: 2026-09-19 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260919_0039"
down_revision = "20260919_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("open_item") as batch_op:
        batch_op.add_column(
            sa.Column("settlement_journal_entry_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("settlement_amount", sa.Numeric(14, 2), nullable=True))
        batch_op.create_foreign_key(
            "fk_open_item_settlement_journal_entry",
            "journal_entry",
            ["settlement_journal_entry_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("open_item") as batch_op:
        batch_op.drop_constraint("fk_open_item_settlement_journal_entry", type_="foreignkey")
        batch_op.drop_column("settlement_amount")
        batch_op.drop_column("settlement_journal_entry_id")
