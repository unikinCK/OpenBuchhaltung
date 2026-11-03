"""Konsolidierte Auswertung: Gruppen, Mitglieder, manuelle Konsolidierungsbuchungen und
Kennzeichen ``business_partner.intercompany_company_id`` (ADR-008).

Revision ID: 20261103_0061
Revises: 20261103_0057
Create Date: 2026-11-03 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_0061"
down_revision = "20261103_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # Nullable REFERENCES-Spalte ohne Tabellen-Rebuild: journal_entry_line verweist auf
        # business_partner, ein Rebuild liefe gegen die Fremdschlüssel der Buchungszeilen.
        op.execute(
            sa.text(
                "ALTER TABLE business_partner ADD COLUMN intercompany_company_id INTEGER "
                "REFERENCES company(id) ON DELETE SET NULL"
            )
        )
    else:
        op.add_column(
            "business_partner",
            sa.Column(
                "intercompany_company_id",
                sa.Integer(),
                sa.ForeignKey("company.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    op.create_table(
        "consolidation_group",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "parent_company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_consolidation_group_tenant_name"),
    )
    op.create_table(
        "consolidation_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("consolidation_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ownership_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("included_from", sa.Date(), nullable=True),
        sa.Column("included_to", sa.Date(), nullable=True),
        sa.UniqueConstraint("group_id", "company_id", name="uq_consolidation_member_group_company"),
        sa.CheckConstraint(
            "ownership_percent > 0 AND ownership_percent <= 100",
            name="ck_consolidation_member_ownership",
        ),
        sa.CheckConstraint(
            "included_from IS NULL OR included_to IS NULL OR included_from <= included_to",
            name="ck_consolidation_member_included_range",
        ),
    )
    op.create_table(
        "consolidation_adjustment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("consolidation_group.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("adjustment_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('capital', 'other')", name="ck_consolidation_adjustment_kind"),
    )
    op.create_index(
        "ix_consolidation_adjustment_group_date",
        "consolidation_adjustment",
        ["group_id", "adjustment_date"],
    )
    op.create_table(
        "consolidation_adjustment_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "adjustment_id",
            sa.Integer(),
            sa.ForeignKey("consolidation_adjustment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("account_code", sa.String(length=20), nullable=False),
        sa.Column("account_name", sa.String(length=255), nullable=False),
        sa.Column("account_type", sa.String(length=30), nullable=False),
        sa.Column("debit_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("credit_amount", sa.Numeric(14, 2), nullable=False),
        sa.UniqueConstraint(
            "adjustment_id", "line_number", name="uq_consolidation_adjustment_line_number"
        ),
        sa.CheckConstraint(
            "debit_amount >= 0 AND credit_amount >= 0",
            name="ck_consolidation_adjustment_line_non_negative",
        ),
        sa.CheckConstraint(
            "(debit_amount > 0 AND credit_amount = 0) OR (credit_amount > 0 AND debit_amount = 0)",
            name="ck_consolidation_adjustment_line_one_sided",
        ),
    )


def downgrade() -> None:
    op.drop_table("consolidation_adjustment_line")
    op.drop_index(
        "ix_consolidation_adjustment_group_date", table_name="consolidation_adjustment"
    )
    op.drop_table("consolidation_adjustment")
    op.drop_table("consolidation_member")
    op.drop_table("consolidation_group")
    if op.get_bind().dialect.name == "sqlite":
        # ALTER TABLE … DROP COLUMN (SQLite ≥ 3.35) statt Rebuild, siehe upgrade().
        op.execute(sa.text("ALTER TABLE business_partner DROP COLUMN intercompany_company_id"))
    else:
        op.drop_column("business_partner", "intercompany_company_id")
