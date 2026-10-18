"""Kontierungsvorschlag am Belegabgleich-Vorschlag: Konto, Steuercode, Partner
sowie die zusätzlich ausgelesenen Belegfelder (USt-IdNr., Beschreibung,
Trinkgeld, Reverse Charge).

Bestehende Vorschläge bleiben ohne Kontierungsvorschlag.

Revision ID: 20261018_0050
Revises: 20261014_0049
Create Date: 2026-10-18 21:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261018_0050"
down_revision = "20261014_0049"
branch_labels = None
depends_on = None

TABLE = "receipt_match_suggestion"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("supplier_vat_id", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("description", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("tip_amount", sa.Numeric(14, 2), nullable=True))
        batch.add_column(
            sa.Column(
                "reverse_charge", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("expense_account_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tax_code_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("partner_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("proposal_source", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("proposal_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_receipt_match_suggestion_expense_account",
            "account",
            ["expense_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_receipt_match_suggestion_tax_code",
            "tax_code",
            ["tax_code_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_receipt_match_suggestion_partner",
            "business_partner",
            ["partner_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint("fk_receipt_match_suggestion_partner", type_="foreignkey")
        batch.drop_constraint("fk_receipt_match_suggestion_tax_code", type_="foreignkey")
        batch.drop_constraint(
            "fk_receipt_match_suggestion_expense_account", type_="foreignkey"
        )
        for column in (
            "proposal_reason",
            "proposal_source",
            "partner_id",
            "tax_code_id",
            "expense_account_id",
            "reverse_charge",
            "tip_amount",
            "description",
            "supplier_vat_id",
        ):
            batch.drop_column(column)
