"""Steuercode-Richtung: tax_code.kind (input = Vorsteuer, output = Umsatzsteuer).

Bestehende Codes werden aus dem Kontotyp des Steuerkontos abgeleitet
(asset = Vorsteuer), ohne Steuerkonto aus dem Kürzel (V… = Vorsteuer).

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
    with op.batch_alter_table("tax_code") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind", sa.String(length=10), nullable=False, server_default="output"
            )
        )
    op.execute(
        sa.text(
            "UPDATE tax_code SET kind = 'input' WHERE "
            "vat_account_id IN (SELECT id FROM account WHERE account_type = 'asset') "
            "OR (vat_account_id IS NULL AND lower(code) LIKE 'v%')"
        )
    )
    with op.batch_alter_table("tax_code") as batch_op:
        batch_op.create_check_constraint(
            "ck_tax_code_kind_known", "kind IN ('input', 'output')"
        )


def downgrade() -> None:
    with op.batch_alter_table("tax_code") as batch_op:
        batch_op.drop_constraint("ck_tax_code_kind_known", type_="check")
        batch_op.drop_column("kind")
