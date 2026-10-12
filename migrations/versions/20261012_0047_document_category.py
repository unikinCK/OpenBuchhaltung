"""Belegkategorien (``document_category``) und ``document.category_id``.

Jede bestehende Gesellschaft erhält die Standardkategorien Rechnung,
Gutschrift, Zahlungsbeleg, Bewirtungsbeleg und Zahlungsanweisung; bestehende
Belege bleiben ohne Kategorie.

Revision ID: 20261012_0047
Revises: 20261011_0046
Create Date: 2026-10-12 10:00:00
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "20261012_0047"
down_revision = "20261011_0046"
branch_labels = None
depends_on = None

# Stand der Standardkategorien zum Zeitpunkt dieser Migration (bewusst nicht
# aus app.services importiert, damit spätere Änderungen sie nicht verändern).
DEFAULT_CATEGORIES = (
    "Rechnung",
    "Gutschrift",
    "Zahlungsbeleg",
    "Bewirtungsbeleg",
    "Zahlungsanweisung",
)


def upgrade() -> None:
    op.create_table(
        "document_category",
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
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("company_id", "name", name="uq_document_category_company_name"),
    )
    with op.batch_alter_table("document") as batch_op:
        batch_op.add_column(sa.Column("category_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_document_category",
            "document_category",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )

    bind = op.get_bind()
    company = sa.table("company", sa.column("id"), sa.column("tenant_id"))
    category = sa.table(
        "document_category",
        sa.column("tenant_id"),
        sa.column("company_id"),
        sa.column("name"),
        sa.column("sort_order"),
        sa.column("is_default"),
        sa.column("is_active"),
        sa.column("created_at"),
    )
    now = datetime.now(timezone.utc)
    rows = [
        {
            "tenant_id": row.tenant_id,
            "company_id": row.id,
            "name": name,
            "sort_order": (index + 1) * 10,
            "is_default": True,
            "is_active": True,
            "created_at": now,
        }
        for row in bind.execute(sa.select(company.c.id, company.c.tenant_id))
        for index, name in enumerate(DEFAULT_CATEGORIES)
    ]
    if rows:
        op.bulk_insert(category, rows)


def downgrade() -> None:
    with op.batch_alter_table("document") as batch_op:
        batch_op.drop_constraint("fk_document_category", type_="foreignkey")
        batch_op.drop_column("category_id")
    op.drop_table("document_category")
