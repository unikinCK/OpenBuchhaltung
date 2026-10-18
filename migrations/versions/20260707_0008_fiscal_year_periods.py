"""Add company.fiscal_year_start_month and period.is_closing.

Ermoeglicht abweichende Wirtschaftsjahre, Rumpfjahre und eine gesonderte
Abschlussperiode (Periode 13) je Wirtschaftsjahr.

Revision ID: 20260707_0008
Revises: 20260706_0007
Create Date: 2026-07-07 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260707_0008"
down_revision = "20260706_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("company") as batch_op:
        batch_op.add_column(
            sa.Column(
                "fiscal_year_start_month",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_check_constraint(
            "ck_company_fiscal_year_start_month",
            "fiscal_year_start_month BETWEEN 1 AND 12",
        )

    with op.batch_alter_table("period") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_closing",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("period") as batch_op:
        batch_op.drop_column("is_closing")

    with op.batch_alter_table("company") as batch_op:
        batch_op.drop_constraint("ck_company_fiscal_year_start_month", type_="check")
        batch_op.drop_column("fiscal_year_start_month")
