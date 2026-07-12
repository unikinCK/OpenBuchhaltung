"""ELSTER: procedure fuer USt-Jahreserklaerung erlauben.

Revision ID: 20260713_0016
Revises: 20260712_0015
Create Date: 2026-07-13 00:30:00
"""

from alembic import op

revision = "20260713_0016"
down_revision = "20260712_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("elster_submission") as batch_op:
        batch_op.drop_constraint("ck_elster_submission_procedure_known", type_="check")
        batch_op.create_check_constraint(
            "ck_elster_submission_procedure_known",
            "procedure IN ('ustva', 'ust_jahreserklaerung')",
        )


def downgrade() -> None:
    with op.batch_alter_table("elster_submission") as batch_op:
        batch_op.drop_constraint("ck_elster_submission_procedure_known", type_="check")
        batch_op.create_check_constraint(
            "ck_elster_submission_procedure_known",
            "procedure IN ('ustva')",
        )
