"""Add user table and tax_code.vat_account_id.

Revision ID: 20260705_0003
Revises: 20260407_0002
Create Date: 2026-07-05 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260705_0003"
down_revision = "20260407_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    with op.batch_alter_table("tax_code") as batch_op:
        batch_op.add_column(sa.Column("vat_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tax_code_vat_account",
            "account",
            ["vat_account_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("tax_code") as batch_op:
        batch_op.drop_constraint("fk_tax_code_vat_account", type_="foreignkey")
        batch_op.drop_column("vat_account_id")

    op.drop_table("user")
