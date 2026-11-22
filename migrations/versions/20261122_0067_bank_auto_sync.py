"""Täglicher automatischer Umsatzabruf eingerichteter Bankzugänge.

Revision ID: 20261122_0067
Revises: 20261108_0066
Create Date: 2026-11-22

Neue Tabelle ``bank_auto_sync``: je FinTS-Zugang, Open-Banking-Zugang oder Wallet eines
Bank-MCP-Servers ein Schalter samt Stand des letzten Abrufs. Die FinTS-PIN für den Abruf
ohne Benutzer liegt nur hier und nur verschlüsselt vor.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261122_0067"
down_revision = "20261108_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_auto_sync",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bank_account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "fints_connection_id",
            sa.Integer(),
            sa.ForeignKey("fints_connection.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "open_banking_connection_id",
            sa.Integer(),
            sa.ForeignKey("open_banking_connection.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "mcp_server_id",
            sa.Integer(),
            sa.ForeignKey("mcp_server.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("mcp_wallet_id", sa.String(length=64), nullable=True),
        sa.Column("mcp_wallet_name", sa.String(length=120), nullable=True),
        sa.Column("secret_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("paused_reason", sa.String(length=30), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=20), nullable=True),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("last_imported", sa.Integer(), nullable=True),
        sa.Column("last_synced_to", sa.Date(), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('fints', 'open_banking', 'mcp')", name="ck_bank_auto_sync_source"
        ),
        sa.UniqueConstraint("fints_connection_id", name="uq_bank_auto_sync_fints_connection"),
        sa.UniqueConstraint(
            "open_banking_connection_id", name="uq_bank_auto_sync_open_banking_connection"
        ),
        sa.UniqueConstraint(
            "company_id", "mcp_server_id", "mcp_wallet_id", name="uq_bank_auto_sync_mcp_wallet"
        ),
    )


def downgrade() -> None:
    op.drop_table("bank_auto_sync")
