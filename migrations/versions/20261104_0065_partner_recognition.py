"""Partnererkennung in automatischen Quellen (Sprint 5a, Schritt 5).

``bank_transaction.counterparty_iban`` nimmt die Gegen-IBAN aus dem Kontoauszug auf; sie gehört
nicht zum Dedup-Hash, damit bereits importierte Umsätze Duplikate bleiben.
``bank_booking_rule.partner_id`` lässt eine Kontierungsregel den Partner setzen.
``receipt_match_suggestion`` merkt sich das Erkennungsergebnis, die IBAN und die Fälligkeit des
Belegs, denn der Belegtext wird nicht gespeichert.

Die Tabellen tragen keine Immutability-Trigger; die Spalten entstehen auf SQLite trotzdem per
``ALTER TABLE`` ohne Tabellen-Rebuild.

Revision ID: 20261104_0065
Revises: 20261103_0064
Create Date: 2026-11-04 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261104_0065"
down_revision = "20261103_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "bank_transaction", sa.Column("counterparty_iban", sa.String(34), nullable=True)
    )
    if bind.dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE bank_booking_rule ADD COLUMN partner_id INTEGER "
            "REFERENCES business_partner (id) ON DELETE RESTRICT"
        )
    else:
        op.add_column(
            "bank_booking_rule",
            sa.Column(
                "partner_id",
                sa.Integer(),
                sa.ForeignKey(
                    "business_partner.id",
                    name="fk_bank_booking_rule_partner_id",
                    ondelete="RESTRICT",
                ),
                nullable=True,
            ),
        )
    op.add_column("receipt_match_suggestion", sa.Column("partner_match", sa.JSON(), nullable=True))
    op.add_column(
        "receipt_match_suggestion", sa.Column("supplier_iban", sa.String(34), nullable=True)
    )
    op.add_column("receipt_match_suggestion", sa.Column("due_date", sa.Date(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_column("receipt_match_suggestion", "due_date")
    op.drop_column("receipt_match_suggestion", "supplier_iban")
    op.drop_column("receipt_match_suggestion", "partner_match")
    if bind.dialect.name == "sqlite":
        # DROP COLUMN geht auf SQLite nur ohne FK-Klausel; die Tabelle hat keine Trigger.
        with op.batch_alter_table("bank_booking_rule", recreate="always") as batch_op:
            batch_op.drop_column("partner_id")
    else:
        op.drop_constraint(
            "fk_bank_booking_rule_partner_id", "bank_booking_rule", type_="foreignkey"
        )
        op.drop_column("bank_booking_rule", "partner_id")
    op.drop_column("bank_transaction", "counterparty_iban")
