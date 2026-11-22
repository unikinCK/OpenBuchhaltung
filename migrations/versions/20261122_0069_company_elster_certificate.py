"""ELSTER-Zertifikat und PIN je Gesellschaft, verschlüsselt.

Revision ID: 20261122_0069
Revises: 20261122_0068
Create Date: 2026-11-22

Bisher galt ein Zertifikat je Instanz (``ELSTER_CERTIFICATE_PATH``/``ELSTER_CERTIFICATE_PIN``).
Jetzt kann jede Gesellschaft ihr eigenes Soft-PSE hinterlegen; Datei und PIN liegen nur
Fernet-verschlüsselt vor (ADR-011). Das Instanz-Zertifikat bleibt Rückfall.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261122_0069"
down_revision = "20261122_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_elster_certificate",
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
        sa.Column("certificate_encrypted", sa.Text(), nullable=False),
        sa.Column("pin_encrypted", sa.Text(), nullable=False),
        sa.Column("alias", sa.String(length=120), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_by", sa.String(length=120), nullable=False),
        sa.UniqueConstraint("company_id", name="uq_company_elster_certificate_company"),
    )


def downgrade() -> None:
    op.drop_table("company_elster_certificate")
