"""KI-Zugang (LLM) je Benutzer: Anbieter, Endpoint, Modell und verschlüsselter API-Key.

Revision ID: 20261009_0043
Revises: 20261008_0042
Create Date: 2026-10-09 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0043"
down_revision = "20261008_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("llm_provider", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("llm_endpoint_url", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("llm_model", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("llm_api_key_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("llm_api_key_last4", sa.String(length=4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("llm_api_key_last4")
        batch_op.drop_column("llm_api_key_encrypted")
        batch_op.drop_column("llm_model")
        batch_op.drop_column("llm_endpoint_url")
        batch_op.drop_column("llm_provider")
