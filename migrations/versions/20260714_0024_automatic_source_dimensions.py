"""Add controlling defaults to fixed assets and payroll employees.

Revision ID: 20260714_0024
Revises: 20260714_0023
Create Date: 2026-07-14 14:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260714_0024"
down_revision = "20260714_0023"
branch_labels = None
depends_on = None


def _add_dimension_columns(table_name: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for column_name in ("cost_center_id", "profit_center_id"):
            op.execute(
                sa.text(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} INTEGER "
                    "REFERENCES controlling_unit(id) ON DELETE RESTRICT"
                )
            )
    else:
        for column_name in ("cost_center_id", "profit_center_id"):
            op.add_column(
                table_name,
                sa.Column(
                    column_name,
                    sa.Integer(),
                    sa.ForeignKey("controlling_unit.id", ondelete="RESTRICT"),
                    nullable=True,
                ),
            )
    op.create_index(f"ix_{table_name}_cost_center", table_name, ["cost_center_id"])
    op.create_index(f"ix_{table_name}_profit_center", table_name, ["profit_center_id"])


def upgrade() -> None:
    _add_dimension_columns("fixed_asset")
    _add_dimension_columns("payroll_employee")


def downgrade() -> None:
    for table_name in ("payroll_employee", "fixed_asset"):
        op.drop_index(f"ix_{table_name}_profit_center", table_name=table_name)
        op.drop_index(f"ix_{table_name}_cost_center", table_name=table_name)
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column("profit_center_id")
            batch_op.drop_column("cost_center_id")
