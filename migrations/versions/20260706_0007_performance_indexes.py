"""Add indexes for report, bank, and OPOS queries.

Revision ID: 20260706_0007
Revises: 20260706_0006
Create Date: 2026-07-06 12:00:00
"""

from alembic import op

revision = "20260706_0007"
down_revision = "20260706_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_account_company_code", "account", ["company_id", "code"])
    op.create_index("ix_journal_entry_company_date", "journal_entry", ["company_id", "entry_date"])
    op.create_index("ix_journal_entry_line_account", "journal_entry_line", ["account_id"])
    op.create_index(
        "ix_journal_entry_line_entry_account",
        "journal_entry_line",
        ["journal_entry_id", "account_id"],
    )
    op.create_index(
        "ix_bank_transaction_company_status_date",
        "bank_transaction",
        ["company_id", "status", "booking_date"],
    )
    op.create_index(
        "ix_bank_transaction_journal_entry", "bank_transaction", ["journal_entry_id"]
    )
    op.create_index(
        "ix_open_item_company_status_due",
        "open_item",
        ["company_id", "status", "due_date", "entry_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_open_item_company_status_due", table_name="open_item")
    op.drop_index("ix_bank_transaction_journal_entry", table_name="bank_transaction")
    op.drop_index("ix_bank_transaction_company_status_date", table_name="bank_transaction")
    op.drop_index("ix_journal_entry_line_entry_account", table_name="journal_entry_line")
    op.drop_index("ix_journal_entry_line_account", table_name="journal_entry_line")
    op.drop_index("ix_journal_entry_company_date", table_name="journal_entry")
    op.drop_index("ix_account_company_code", table_name="account")
