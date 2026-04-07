"""Add 4-level account hierarchy fields.

Revision ID: 20260407_0002
Revises: 20260328_0001
Create Date: 2026-04-07 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260407_0002"
down_revision = "20260328_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account", sa.Column("hierarchy_level", sa.Integer(), nullable=True))
    op.add_column("account", sa.Column("level_1", sa.String(length=1), nullable=True))
    op.add_column("account", sa.Column("level_2", sa.String(length=1), nullable=True))
    op.add_column("account", sa.Column("level_3", sa.String(length=1), nullable=True))
    op.add_column("account", sa.Column("level_4", sa.String(length=1), nullable=True))
    op.add_column("account", sa.Column("parent_account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_account_parent_account_id",
        "account",
        "account",
        ["parent_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    bind = op.get_bind()
    account_table = sa.table(
        "account",
        sa.column("id", sa.Integer()),
        sa.column("company_id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("hierarchy_level", sa.Integer()),
        sa.column("level_1", sa.String()),
        sa.column("level_2", sa.String()),
        sa.column("level_3", sa.String()),
        sa.column("level_4", sa.String()),
        sa.column("parent_account_id", sa.Integer()),
    )

    rows = bind.execute(sa.select(account_table.c.id, account_table.c.company_id, account_table.c.code))
    cache: dict[tuple[int, str], int] = {}
    materialized_rows = list(rows)
    for row in materialized_rows:
        cache[(row.company_id, row.code)] = row.id

    for row in materialized_rows:
        digits = "".join(ch for ch in row.code if ch.isdigit())
        padded = (digits[:4]).ljust(4, "0")
        if padded[3] != "0":
            hierarchy_level = 4
            candidates = [f"{padded[:3]}0", f"{padded[:2]}00", f"{padded[0]}000"]
        elif padded[2] != "0":
            hierarchy_level = 3
            candidates = [f"{padded[:2]}00", f"{padded[0]}000"]
        elif padded[1] != "0":
            hierarchy_level = 2
            candidates = [f"{padded[0]}000"]
        else:
            hierarchy_level = 1
            candidates = []

        parent_account_id = None
        for candidate in candidates:
            parent_account_id = cache.get((row.company_id, candidate))
            if parent_account_id is not None:
                break

        bind.execute(
            account_table.update()
            .where(account_table.c.id == row.id)
            .values(
                hierarchy_level=hierarchy_level,
                level_1=padded[0],
                level_2=padded[1],
                level_3=padded[2],
                level_4=padded[3],
                parent_account_id=parent_account_id,
            )
        )

    op.alter_column("account", "hierarchy_level", nullable=False)
    op.alter_column("account", "level_1", nullable=False)
    op.alter_column("account", "level_2", nullable=False)
    op.alter_column("account", "level_3", nullable=False)
    op.alter_column("account", "level_4", nullable=False)
    op.create_check_constraint(
        "ck_account_hierarchy_level_range",
        "account",
        "hierarchy_level BETWEEN 1 AND 4",
    )


def downgrade() -> None:
    op.drop_constraint("ck_account_hierarchy_level_range", "account", type_="check")
    op.drop_constraint("fk_account_parent_account_id", "account", type_="foreignkey")
    op.drop_column("account", "parent_account_id")
    op.drop_column("account", "level_4")
    op.drop_column("account", "level_3")
    op.drop_column("account", "level_2")
    op.drop_column("account", "level_1")
    op.drop_column("account", "hierarchy_level")
