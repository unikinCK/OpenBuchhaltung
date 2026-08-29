"""Bankumsatz-Härtung: Status-CHECK und Index auf die Buchungsverknüpfung.

Kein Unique-Index auf journal_entry_id: Teilzahlungen verknüpfen über
settle_open_item legitim mehrere Bankumsätze mit derselben Buchung.

Revision ID: 20260829_0031
Revises: 20260829_0030
Create Date: 2026-08-29 11:00:00
"""

from alembic import op

revision = "20260829_0031"
down_revision = "20260829_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bank_transaction") as batch:
        batch.create_check_constraint(
            "ck_bank_tx_status_known", "status IN ('open', 'matched', 'booked')"
        )
    op.create_index("ix_bank_tx_journal_entry", "bank_transaction", ["journal_entry_id"])


def downgrade() -> None:
    op.drop_index("ix_bank_tx_journal_entry", table_name="bank_transaction")
    with op.batch_alter_table("bank_transaction") as batch:
        batch.drop_constraint("ck_bank_tx_status_known", type_="check")
