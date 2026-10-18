"""Payroll PAP input fields.

Revision ID: 20260712_0015
Revises: 20260712_0014
Create Date: 2026-07-12 23:59:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0015"
down_revision = "20260712_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payroll_employee") as batch_op:
        batch_op.add_column(sa.Column("birth_date", sa.Date()))
        batch_op.add_column(
            sa.Column("tax_class", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("child_allowances", sa.Numeric(4, 1), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("federal_state", sa.String(length=2)))
        batch_op.add_column(
            sa.Column("main_employment", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.create_check_constraint(
            "ck_payroll_employee_tax_class", "tax_class BETWEEN 1 AND 6"
        )


def downgrade() -> None:
    with op.batch_alter_table("payroll_employee") as batch_op:
        batch_op.drop_constraint("ck_payroll_employee_tax_class", type_="check")
        batch_op.drop_column("main_employment")
        batch_op.drop_column("federal_state")
        batch_op.drop_column("child_allowances")
        batch_op.drop_column("tax_class")
        batch_op.drop_column("birth_date")
