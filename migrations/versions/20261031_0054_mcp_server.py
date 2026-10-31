"""Externe MCP-Server für Obi (``mcp_server``) und ihre Zugänge (``mcp_credential``,
zentral oder je Benutzer).

Revision ID: 20261031_0054
Revises: 20261031_0053
Create Date: 2026-10-31 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261031_0054"
down_revision = "20261031_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_server",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("tool_prefix", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("auth_type", sa.String(length=10), nullable=False),
        sa.Column("credential_scope", sa.String(length=10), nullable=False),
        sa.Column("token_header", sa.String(length=64), nullable=True),
        sa.Column("oauth_client_id", sa.String(length=255), nullable=True),
        sa.Column("oauth_client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("oauth_scope", sa.String(length=500), nullable=True),
        sa.Column("oauth_authorization_url", sa.String(length=500), nullable=True),
        sa.Column("oauth_token_url", sa.String(length=500), nullable=True),
        sa.Column("oauth_registration_encrypted", sa.Text(), nullable=True),
        sa.Column("confirm_mode", sa.String(length=10), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("tools_cache", sa.JSON(), nullable=True),
        sa.Column("server_info", sa.JSON(), nullable=True),
        sa.Column("tools_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tools_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tool_prefix", name="uq_mcp_server_tool_prefix"),
        sa.CheckConstraint(
            "auth_type IN ('none', 'token', 'oauth')", name="ck_mcp_server_auth_type"
        ),
        sa.CheckConstraint(
            "credential_scope IN ('global', 'user')", name="ck_mcp_server_credential_scope"
        ),
        sa.CheckConstraint(
            "confirm_mode IN ('write', 'all', 'none')", name="ck_mcp_server_confirm_mode"
        ),
        sa.CheckConstraint(
            "timeout_seconds BETWEEN 5 AND 300", name="ck_mcp_server_timeout_seconds"
        ),
    )
    op.create_table(
        "mcp_credential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("mcp_server.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column("secret_last4", sa.String(length=4), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("auth_state", sa.String(length=64), nullable=True),
        sa.Column("auth_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "auth_started_by_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("pending_encrypted", sa.Text(), nullable=True),
        sa.Column("connected_by", sa.String(length=120), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("server_id", "user_id", name="uq_mcp_credential_server_user"),
        sa.UniqueConstraint("auth_state", name="uq_mcp_credential_auth_state"),
        sa.CheckConstraint("kind IN ('token', 'oauth')", name="ck_mcp_credential_kind"),
    )
    op.create_index(
        "uq_mcp_credential_global",
        "mcp_credential",
        ["server_id"],
        unique=True,
        sqlite_where=sa.text("user_id IS NULL"),
        postgresql_where=sa.text("user_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_mcp_credential_global", table_name="mcp_credential")
    op.drop_table("mcp_credential")
    op.drop_table("mcp_server")
