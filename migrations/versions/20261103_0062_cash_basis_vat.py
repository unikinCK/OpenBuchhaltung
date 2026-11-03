"""Istversteuerung: Versteuerungsart je Gesellschaft und Zahlungen je offenem Posten.

``open_item_settlement`` hält jeden Ausgleich mit Datum und Betrag fest; bisher stand am Posten
nur der letzte Ausgleich. Bestehende Ausgleiche werden übernommen: die letzte Ausgleichsbuchung
mit ihrem Betrag und der Rest als eine Zeile ohne Buchung.

Revision ID: 20261103_0062
Revises: 20261103_0060
Create Date: 2026-11-03 12:00:00
"""

from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision = "20261103_0062"
down_revision = "20261103_0060"
branch_labels = None
depends_on = None


def _as_date(value) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _decimal(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0.00")


def upgrade() -> None:
    op.add_column("company", sa.Column("vat_cash_basis_from", sa.Date(), nullable=True))
    op.add_column("company", sa.Column("vat_cash_basis_until", sa.Date(), nullable=True))

    settlement = op.create_table(
        "open_item_settlement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "open_item_id",
            sa.Integer(),
            sa.ForeignKey("open_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("settlement_date", sa.Date(), nullable=False),
        sa.Column(
            "journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entry.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "bank_transaction_id",
            sa.Integer(),
            sa.ForeignKey("bank_transaction.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_on", sa.Date(), nullable=True),
        sa.Column(
            "reversal_journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entry.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("amount > 0", name="ck_open_item_settlement_amount_positive"),
    )
    op.create_index(
        "ix_open_item_settlement_item", "open_item_settlement", ["open_item_id", "settlement_date"]
    )
    op.create_index(
        "ix_open_item_settlement_entry", "open_item_settlement", ["journal_entry_id"]
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT item.id, item.tenant_id, item.company_id, item.original_amount, item.open_amount,
                   item.settlement_journal_entry_id, item.settlement_amount,
                   item.bank_transaction_id, item.settled_at, item.settled_by, item.entry_date,
                   item.created_at, entry.entry_date AS settlement_entry_date,
                   tx.booking_date AS booking_date
            FROM open_item AS item
            LEFT JOIN journal_entry AS entry ON entry.id = item.settlement_journal_entry_id
            LEFT JOIN bank_transaction AS tx ON tx.id = item.bank_transaction_id
            WHERE item.open_amount < item.original_amount
            """
        )
    ).mappings()
    new_rows = []
    for row in rows:
        settled = _decimal(row["original_amount"]) - _decimal(row["open_amount"])
        linked = (
            _decimal(row["settlement_amount"])
            if row["settlement_journal_entry_id"]
            else Decimal("0.00")
        )
        linked = min(linked, settled)
        fallback_date = (
            _as_date(row["booking_date"])
            or _as_date(row["settled_at"])
            or _as_date(row["entry_date"])
        )
        common = {
            "tenant_id": row["tenant_id"],
            "company_id": row["company_id"],
            "open_item_id": row["id"],
            "created_by": row["settled_by"] or "migration",
            "created_at": _as_datetime(row["created_at"]),
            "reversed_on": None,
            "reversal_journal_entry_id": None,
        }
        if linked > 0:
            new_rows.append(
                {
                    **common,
                    "amount": linked,
                    "settlement_date": _as_date(row["settlement_entry_date"]) or fallback_date,
                    "journal_entry_id": row["settlement_journal_entry_id"],
                    "bank_transaction_id": row["bank_transaction_id"],
                }
            )
        if settled > linked:
            new_rows.append(
                {
                    **common,
                    "amount": settled - linked,
                    "settlement_date": fallback_date,
                    "journal_entry_id": None,
                    "bank_transaction_id": row["bank_transaction_id"] if linked == 0 else None,
                }
            )
    if new_rows:
        op.bulk_insert(settlement, new_rows)


def downgrade() -> None:
    op.drop_index("ix_open_item_settlement_entry", table_name="open_item_settlement")
    op.drop_index("ix_open_item_settlement_item", table_name="open_item_settlement")
    op.drop_table("open_item_settlement")
    with op.batch_alter_table("company") as batch_op:
        batch_op.drop_column("vat_cash_basis_until")
        batch_op.drop_column("vat_cash_basis_from")
