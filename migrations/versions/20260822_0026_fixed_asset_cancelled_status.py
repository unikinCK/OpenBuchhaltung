"""Allow status 'cancelled' for fixed assets (Storno fehlerhafter Anlagen).

Revision ID: 20260822_0026
Revises: 20260822_0025
Create Date: 2026-08-22 19:10:00
"""

from alembic import op

revision = "20260822_0026"
down_revision = "20260822_0025"
branch_labels = None
depends_on = None

_OLD = "status IN ('active', 'disposed', 'fully_depreciated')"
_NEW = "status IN ('active', 'disposed', 'fully_depreciated', 'cancelled')"


def upgrade() -> None:
    with op.batch_alter_table("fixed_asset") as batch:
        batch.drop_constraint("ck_fixed_asset_status_known", type_="check")
        batch.create_check_constraint("ck_fixed_asset_status_known", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("fixed_asset") as batch:
        batch.drop_constraint("ck_fixed_asset_status_known", type_="check")
        batch.create_check_constraint("ck_fixed_asset_status_known", _OLD)
