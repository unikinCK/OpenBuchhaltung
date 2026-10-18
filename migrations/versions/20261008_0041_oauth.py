"""OAuth 2.1 für den MCP-Endpunkt: Clients (DCR), Autorisierungscodes, Grants.

Revision ID: 20261008_0041
Revises: 20260919_0040
Create Date: 2026-10-08 09:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261008_0041"
down_revision = "20260919_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_client",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("client_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oauth_authorization_code",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("oauth_client.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=True),
        sa.Column("resource", sa.String(length=500), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "oauth_grant",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("oauth_client.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=True, unique=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.String(length=200), nullable=True),
        sa.Column("resource", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_oauth_grant_user", "oauth_grant", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_grant_user", table_name="oauth_grant")
    op.drop_table("oauth_grant")
    op.drop_table("oauth_authorization_code")
    op.drop_table("oauth_client")
