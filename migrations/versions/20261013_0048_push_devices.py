"""Push-Mitteilungen: Mobilgeräte (``push_device``) und Ausgangswarteschlange
(``push_notification``).

Revision ID: 20261013_0048
Revises: 20261012_0047
Create Date: 2026-10-13 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261013_0048"
down_revision = "20261012_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_device",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("device_token", sa.String(length=200), nullable=False, unique=True),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("device_name", sa.String(length=100), nullable=True),
        sa.Column("app_version", sa.String(length=40), nullable=True),
        sa.Column("server_url", sa.String(length=500), nullable=True),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_push_device_user", "push_device", ["user_id"])

    op.create_table(
        "push_notification",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "device_id",
            sa.Integer(),
            sa.ForeignKey("push_device.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("event", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("body", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_push_notification_status", "push_notification", ["status", "id"])
    op.create_index(
        "ix_push_notification_company_event",
        "push_notification",
        ["company_id", "event", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_push_notification_company_event", table_name="push_notification")
    op.drop_index("ix_push_notification_status", table_name="push_notification")
    op.drop_table("push_notification")
    op.drop_index("ix_push_device_user", table_name="push_device")
    op.drop_table("push_device")
