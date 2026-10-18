"""KI-Chat: Unterhaltungen und Nachrichten.

Revision ID: 20260830_0036
Revises: 20260830_0035
Create Date: 2026-08-30 16:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0036"
down_revision = "20260830_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_conversation",
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
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("chat_conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_chat_message_conversation_id", "chat_message", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_chat_message_conversation_id", table_name="chat_message")
    op.drop_table("chat_message")
    op.drop_table("chat_conversation")
