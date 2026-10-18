"""Link income-tax snapshots to their accounting fiscal year.

Revision ID: 20260713_0018
Revises: 20260713_0017
Create Date: 2026-07-13 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260713_0018"
down_revision = "20260713_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("income_tax_return") as batch_op:
        batch_op.add_column(sa.Column("fiscal_year_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_income_tax_return_fiscal_year",
            "fiscal_year",
            ["fiscal_year_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_income_tax_return_fiscal_year_id", ["fiscal_year_id"], unique=False
        )

    op.execute(
        sa.text(
            "UPDATE income_tax_return "
            "SET fiscal_year_id = ("
            "SELECT fiscal_year.id FROM fiscal_year "
            "WHERE fiscal_year.company_id = income_tax_return.company_id "
            "AND fiscal_year.start_date = income_tax_return.date_from "
            "AND fiscal_year.end_date = income_tax_return.date_to"
            ")"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("income_tax_return") as batch_op:
        batch_op.drop_index("ix_income_tax_return_fiscal_year_id")
        batch_op.drop_constraint("fk_income_tax_return_fiscal_year", type_="foreignkey")
        batch_op.drop_column("fiscal_year_id")
