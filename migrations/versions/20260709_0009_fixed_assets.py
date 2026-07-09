"""Add fixed_asset and depreciation_entry tables (Anlagenbuchhaltung).

Revision ID: 20260709_0009
Revises: 20260707_0008
Create Date: 2026-07-09 08:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260709_0009"
down_revision = "20260707_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fixed_asset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("asset_number", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("acquisition_date", sa.Date(), nullable=False),
        sa.Column("in_service_date", sa.Date(), nullable=False),
        sa.Column("acquisition_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("useful_life_months", sa.Integer(), nullable=True),
        sa.Column("degressive_rate", sa.Numeric(5, 2), nullable=True),
        sa.Column("total_units", sa.Numeric(14, 2), nullable=True),
        sa.Column("residual_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("keep_memo_value", sa.Boolean(), nullable=False),
        sa.Column("asset_account_id", sa.Integer(), nullable=False),
        sa.Column("depreciation_account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("disposal_date", sa.Date(), nullable=True),
        sa.Column("disposal_proceeds", sa.Numeric(14, 2), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("acquisition_cost > 0", name="ck_fixed_asset_cost_positive"),
        sa.CheckConstraint("residual_value >= 0", name="ck_fixed_asset_residual_non_negative"),
        sa.CheckConstraint(
            "method IN ('linear', 'degressive', 'leistung', 'gwg', 'sammelposten', 'manuell')",
            name="ck_fixed_asset_method_known",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disposed', 'fully_depreciated')",
            name="ck_fixed_asset_status_known",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_account_id"], ["account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["depreciation_account_id"], ["account.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "asset_number", name="uq_fixed_asset_company_number"),
    )
    op.create_index(
        "ix_fixed_asset_company", "fixed_asset", ["company_id", "status"], unique=False
    )

    op.create_table(
        "depreciation_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("fixed_asset_id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("depreciation_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("book_value_before", sa.Numeric(14, 2), nullable=False),
        sa.Column("book_value_after", sa.Numeric(14, 2), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_depreciation_amount_positive"),
        sa.CheckConstraint(
            "kind IN ('planmaessig', 'ausserplanmaessig', 'abgang')",
            name="ck_depreciation_kind_known",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fixed_asset_id"], ["fixed_asset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"], ["journal_entry.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fixed_asset_id", "fiscal_year", "kind", name="uq_depreciation_asset_year_kind"
        ),
    )


def downgrade() -> None:
    op.drop_table("depreciation_entry")
    op.drop_index("ix_fixed_asset_company", table_name="fixed_asset")
    op.drop_table("fixed_asset")
