"""MCP-Proxy und Firmenwissen über MCP: ``mcp_server.proxy_mode`` (Werkzeuge über den
MCP-Zugang von OpenBuchhaltung weiterreichen) und ``company.knowledge_mcp_enabled``.

Bewusst ohne ``batch_alter_table``: SQLite würde ``mcp_server`` dafür neu anlegen, und mit
``PRAGMA foreign_keys=ON`` löschte das Entfernen der alten Tabelle die Zugänge
(``mcp_credential``, ON DELETE CASCADE). Den CHECK gibt es deshalb nur auf PostgreSQL;
die App prüft den Wert ohnehin.

Revision ID: 20261031_0055
Revises: 20261031_0054
Create Date: 2026-10-31 20:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261031_0055"
down_revision = "20261031_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("proxy_mode", sa.String(length=10), nullable=False, server_default="off"),
    )
    if op.get_bind().dialect.name != "sqlite":
        op.create_check_constraint(
            "ck_mcp_server_proxy_mode", "mcp_server", "proxy_mode IN ('off', 'read', 'all')"
        )
    op.add_column(
        "company",
        sa.Column(
            "knowledge_mcp_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("company", "knowledge_mcp_enabled")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_mcp_server_proxy_mode", "mcp_server", type_="check")
    op.drop_column("mcp_server", "proxy_mode")
