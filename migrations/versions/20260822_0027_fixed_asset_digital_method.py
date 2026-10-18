"""Allow method 'digital' for fixed assets (digitale Wirtschaftsgüter, BMF 22.02.2022).

Revision ID: 20260822_0027
Revises: 20260822_0026
Create Date: 2026-08-22 19:45:00
"""

from alembic import op

revision = "20260822_0027"
down_revision = "20260822_0026"
branch_labels = None
depends_on = None

_OLD = "method IN ('linear', 'degressive', 'leistung', 'gwg', 'sammelposten', 'manuell')"
_NEW = (
    "method IN ('linear', 'degressive', 'leistung', 'gwg', 'sammelposten', "
    "'manuell', 'digital')"
)


def upgrade() -> None:
    with op.batch_alter_table("fixed_asset") as batch:
        batch.drop_constraint("ck_fixed_asset_method_known", type_="check")
        batch.create_check_constraint("ck_fixed_asset_method_known", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("fixed_asset") as batch:
        batch.drop_constraint("ck_fixed_asset_method_known", type_="check")
        batch.create_check_constraint("ck_fixed_asset_method_known", _OLD)
