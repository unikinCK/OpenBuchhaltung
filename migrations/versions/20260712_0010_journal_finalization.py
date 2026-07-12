"""GoBD: Festschreibung und Storno-Referenz für Journalbuchungen.

Revision ID: 20260712_0010
Revises: 20260709_0009
Create Date: 2026-07-12 11:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0010"
down_revision = "20260709_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("journal_entry") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_finalized",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("finalized_by", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("reversal_of_id", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint("uq_journal_entry_reversal_of", ["reversal_of_id"])
        batch_op.create_foreign_key(
            "fk_journal_entry_reversal_of",
            "journal_entry",
            ["reversal_of_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("journal_entry") as batch_op:
        batch_op.drop_constraint("fk_journal_entry_reversal_of", type_="foreignkey")
        batch_op.drop_constraint("uq_journal_entry_reversal_of", type_="unique")
        batch_op.drop_column("reversal_of_id")
        batch_op.drop_column("finalized_by")
        batch_op.drop_column("finalized_at")
        batch_op.drop_column("is_finalized")
