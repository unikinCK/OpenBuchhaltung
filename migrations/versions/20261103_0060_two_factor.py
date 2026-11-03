"""Zwei-Faktor-Anmeldung (TOTP): verschlüsseltes Secret und letzter Zeitschritt am Benutzer,
Tabelle ``two_factor_recovery_code`` für die gehashten Wiederherstellungscodes.

Revision ID: 20261103_0060
Revises: 20261103_0057
Create Date: 2026-11-03 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_0060"
down_revision = "20261103_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("totp_secret_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("totp_last_step", sa.Integer(), nullable=True))

    op.create_table(
        "two_factor_recovery_code",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "user_id", "code_hash", name="uq_two_factor_recovery_code_user_hash"
        ),
    )


def downgrade() -> None:
    op.drop_table("two_factor_recovery_code")
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("totp_last_step")
        batch_op.drop_column("totp_enabled_at")
        batch_op.drop_column("totp_secret_encrypted")
