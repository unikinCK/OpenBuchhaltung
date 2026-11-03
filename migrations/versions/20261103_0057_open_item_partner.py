"""Geschäftspartner am offenen Posten (Sprint 5a, Schritt 3).

``open_item`` ist nicht durch Immutability-Trigger geschützt, die Spalte wird auf
SQLite trotzdem per ``ALTER TABLE … REFERENCES`` ohne Tabellen-Rebuild angelegt
(wie ``journal_entry_line.partner_id``).

Revision ID: 20261103_0057
Revises: 20261102_0056
Create Date: 2026-11-03 09:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_0057"
down_revision = "20261102_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE open_item ADD COLUMN partner_id INTEGER "
            "REFERENCES business_partner (id) ON DELETE RESTRICT"
        )
    else:
        op.add_column(
            "open_item",
            sa.Column(
                "partner_id",
                sa.Integer(),
                sa.ForeignKey(
                    "business_partner.id",
                    name="fk_open_item_partner_id",
                    ondelete="RESTRICT",
                ),
                nullable=True,
            ),
        )
    op.create_index("ix_open_item_partner_id", "open_item", ["partner_id"])


def downgrade() -> None:
    op.drop_index("ix_open_item_partner_id", table_name="open_item")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # DROP COLUMN geht auf SQLite nur ohne FK-Klausel; deshalb per Rebuild, open_item hat
        # keine Immutability-Trigger.
        with op.batch_alter_table("open_item", recreate="always") as batch_op:
            batch_op.drop_column("partner_id")
    else:
        op.drop_constraint("fk_open_item_partner_id", "open_item", type_="foreignkey")
        op.drop_column("open_item", "partner_id")
