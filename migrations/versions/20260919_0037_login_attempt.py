"""Login-Fehlversuche in der Datenbank (prozessübergreifendes Rate-Limit).

Revision ID: 20260919_0037
Revises: 20260830_0036
Create Date: 2026-09-19 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260919_0037"
down_revision = "20260830_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("remote_addr", sa.String(length=64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_login_attempt_username_addr", "login_attempt", ["username", "remote_addr"]
    )
    op.create_index("ix_login_attempt_attempted_at", "login_attempt", ["attempted_at"])


def downgrade() -> None:
    op.drop_index("ix_login_attempt_attempted_at", table_name="login_attempt")
    op.drop_index("ix_login_attempt_username_addr", table_name="login_attempt")
    op.drop_table("login_attempt")
