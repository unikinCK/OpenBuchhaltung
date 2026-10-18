"""ELSTER: Uebermittlungsprotokolle fuer Test-/Produktionsversand.

Revision ID: 20260712_0012
Revises: 20260712_0011
Create Date: 2026-07-12 21:35:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0012"
down_revision = "20260712_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "elster_submission",
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
            "vat_return_id",
            sa.Integer(),
            sa.ForeignKey("vat_return.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("procedure", sa.String(length=20), nullable=False, server_default="ustva"),
        sa.Column("environment", sa.String(length=20), nullable=False, server_default="test"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="created"),
        sa.Column("transport", sa.String(length=20), nullable=False, server_default="mock"),
        sa.Column("certificate_alias", sa.String(length=120)),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_xml", sa.Text(), nullable=False),
        sa.Column("response_protocol", sa.Text()),
        sa.Column("transfer_ticket", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.CheckConstraint(
            "procedure IN ('ustva')", name="ck_elster_submission_procedure_known"
        ),
        sa.CheckConstraint(
            "environment IN ('test', 'production')",
            name="ck_elster_submission_environment_known",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'transmitted', 'failed')",
            name="ck_elster_submission_status_known",
        ),
        sa.CheckConstraint(
            "transport IN ('mock', 'eric')", name="ck_elster_submission_transport_known"
        ),
    )
    op.create_index(
        "ix_elster_submission_company_created",
        "elster_submission",
        ["company_id", "created_at"],
    )
    op.create_index(
        "ix_elster_submission_vat_return",
        "elster_submission",
        ["vat_return_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_elster_submission_vat_return", table_name="elster_submission")
    op.drop_index("ix_elster_submission_company_created", table_name="elster_submission")
    op.drop_table("elster_submission")
