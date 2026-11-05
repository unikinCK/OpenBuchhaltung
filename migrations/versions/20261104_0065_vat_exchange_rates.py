"""Umsatzsteuer-Umrechnungskurse je Monat und Umsatzsteuer zum Kurs des Zahlungsmonats.

Revision ID: 20261104_0065
Revises: 20261103_0064
Create Date: 2026-11-04

Neue Tabelle ``vat_exchange_rate``; am Ausgleich Geldeingang, gemeldeter Monatskurs und die
Buchungen der Steuerdifferenz. Zahlungen aus schon festgehaltenen Zeiträumen behalten den Kurs der
Rechnung, zu dem sie gemeldet wurden: Gemeldete Zeiträume dürfen sich nicht ändern (ADR-010).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261104_0065"
down_revision = "20261103_0064"
branch_labels = None
depends_on = None

_ENTRY_COLUMNS = {
    "open_item_settlement": (
        ("vat_fx_entry_id", "fk_open_item_settlement_vat_fx_entry"),
        ("vat_fx_reversal_entry_id", "fk_open_item_settlement_vat_fx_reversal_entry"),
    ),
    "vat_return": (("fx_journal_entry_id", "fk_vat_return_fx_entry"),),
}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "vat_exchange_rate",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("rate_month", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(14, 6), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "currency_code",
            "rate_month",
            name="uq_vat_exchange_rate_tenant_currency_month",
        ),
        sa.CheckConstraint("rate > 0", name="ck_vat_exchange_rate_positive"),
    )

    # Nullable Spalten ohne Tabellen-Rebuild.
    op.add_column("open_item_settlement", sa.Column("received_on", sa.Date(), nullable=True))
    op.add_column("open_item_settlement", sa.Column("vat_rate", sa.Numeric(14, 6), nullable=True))
    for table, columns in _ENTRY_COLUMNS.items():
        for column, constraint in columns:
            if dialect == "sqlite":
                op.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} INTEGER "
                    "REFERENCES journal_entry (id) ON DELETE SET NULL"
                )
            else:
                op.add_column(
                    table,
                    sa.Column(
                        column,
                        sa.Integer(),
                        sa.ForeignKey("journal_entry.id", name=constraint, ondelete="SET NULL"),
                        nullable=True,
                    ),
                )

    # Bis hierher gemeldet: zum Kurs der Rechnung, sei es die Zahlung oder ihr Storno. Der gilt für
    # diese Zahlungen weiter, auch für einen späteren Storno, und ihre Steuerdifferenz ist null.
    op.execute(
        """
        UPDATE open_item_settlement
        SET vat_rate = (
            SELECT open_item.exchange_rate
            FROM open_item
            WHERE open_item.id = open_item_settlement.open_item_id
        )
        WHERE EXISTS (
            SELECT 1
            FROM open_item
            JOIN company ON company.id = open_item.company_id
            WHERE open_item.id = open_item_settlement.open_item_id
              AND open_item.exchange_rate IS NOT NULL
              AND company.vat_cash_basis_from IS NOT NULL
        )
        AND EXISTS (
            SELECT 1
            FROM vat_return
            WHERE vat_return.company_id = open_item_settlement.company_id
              AND vat_return.created_at >= open_item_settlement.created_at
              AND (
                  (
                      vat_return.date_from <= open_item_settlement.settlement_date
                      AND vat_return.date_to >= open_item_settlement.settlement_date
                  )
                  OR (
                      vat_return.date_from <= open_item_settlement.reversed_on
                      AND vat_return.date_to >= open_item_settlement.reversed_on
                  )
              )
        )
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite entfernt Spalten mit Fremdschlüssel nur per Rebuild.
        with op.batch_alter_table("open_item_settlement", recreate="always") as batch_op:
            for column in (
                "vat_fx_reversal_entry_id",
                "vat_fx_entry_id",
                "vat_rate",
                "received_on",
            ):
                batch_op.drop_column(column)
        with op.batch_alter_table("vat_return", recreate="always") as batch_op:
            batch_op.drop_column("fx_journal_entry_id")
    else:
        for table, columns in _ENTRY_COLUMNS.items():
            for column, constraint in columns:
                op.drop_constraint(constraint, table, type_="foreignkey")
                op.drop_column(table, column)
        op.drop_column("open_item_settlement", "vat_rate")
        op.drop_column("open_item_settlement", "received_on")
    op.drop_table("vat_exchange_rate")
