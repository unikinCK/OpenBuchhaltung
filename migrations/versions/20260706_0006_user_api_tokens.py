"""Add user API token fields.

Revision ID: 20260706_0006
Revises: 20260706_0005
Create Date: 2026-07-06 11:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260706_0006"
down_revision = "20260706_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("api_token_hash", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("api_token_last4", sa.String(length=4), nullable=True))
        batch_op.create_unique_constraint("uq_user_api_token_hash", ["api_token_hash"])


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_constraint("uq_user_api_token_hash", type_="unique")
        batch_op.drop_column("api_token_last4")
        batch_op.drop_column("api_token_hash")
