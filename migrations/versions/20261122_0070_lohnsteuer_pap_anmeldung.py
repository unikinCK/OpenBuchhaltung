"""Lohnsteuer nach amtlichem PAP und Lohnsteuer-Anmeldung.

Revision ID: 20261122_0070
Revises: 20261122_0069
Create Date: 2026-11-22

Mitarbeiter bekommen die Merkmale, die der Programmablaufplan (PAP) des BMF braucht
(Berechnungsart, Kirchensteuermerkmal, Krankenversicherung, Pflegeversicherung,
Renten-/Arbeitslosenversicherungspflicht, Frei-/Hinzurechnungsbetrag, Faktor).
Bestehende Mitarbeiter bleiben bei den manuellen Raten (``tax_calculation = manual``).
Lohnlaufzeilen halten das Kirchensteuermerkmal fest; neue Tabelle ``wage_tax_return``
für festgehaltene Lohnsteuer-Anmeldungen.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261122_0070"
down_revision = "20261122_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payroll_employee") as batch:
        batch.add_column(
            sa.Column(
                "tax_calculation", sa.String(length=20), nullable=False, server_default="manual"
            )
        )
        batch.add_column(sa.Column("church_denomination", sa.String(length=2), nullable=True))
        batch.add_column(
            sa.Column(
                "health_insurance_additional_rate",
                sa.Numeric(5, 2),
                nullable=False,
                server_default="2.90",
            )
        )
        batch.add_column(
            sa.Column(
                "private_health_insurance",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column(
                "private_health_premium", sa.Numeric(14, 2), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column(
                "private_health_employer_subsidy",
                sa.Numeric(14, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "care_insurance_children", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column(
                "pension_insurance_compulsory",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(
            sa.Column(
                "unemployment_insurance_compulsory",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(
            sa.Column(
                "monthly_tax_allowance", sa.Numeric(14, 2), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column(
                "monthly_tax_addition", sa.Numeric(14, 2), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("tax_factor", sa.Numeric(4, 3), nullable=True))
        batch.create_check_constraint(
            "ck_payroll_employee_tax_calculation_known",
            "tax_calculation IN ('manual', 'pap')",
        )
        batch.create_check_constraint(
            "ck_payroll_employee_care_children_nonneg", "care_insurance_children >= 0"
        )

    with op.batch_alter_table("payroll_run_line") as batch:
        batch.add_column(sa.Column("church_denomination", sa.String(length=2), nullable=True))

    op.create_table(
        "wage_tax_return",
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
        sa.Column("period_label", sa.String(length=10), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("correction_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("employee_count", sa.Integer(), nullable=False),
        sa.Column("kennzahlen", sa.JSON(), nullable=False),
        sa.Column("payroll_run_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.UniqueConstraint(
            "company_id",
            "period_label",
            "correction_number",
            name="uq_wage_tax_return_company_period_correction",
        ),
        sa.CheckConstraint("date_from <= date_to", name="ck_wage_tax_return_date_range"),
        sa.CheckConstraint(
            "status IN ('erstellt', 'uebermittelt')", name="ck_wage_tax_return_status_known"
        ),
        sa.CheckConstraint(
            "correction_number >= 0", name="ck_wage_tax_return_correction_nonneg"
        ),
        sa.CheckConstraint("employee_count >= 0", name="ck_wage_tax_return_employees_nonneg"),
    )


def downgrade() -> None:
    op.drop_table("wage_tax_return")
    with op.batch_alter_table("payroll_run_line") as batch:
        batch.drop_column("church_denomination")
    with op.batch_alter_table("payroll_employee") as batch:
        batch.drop_constraint("ck_payroll_employee_care_children_nonneg", type_="check")
        batch.drop_constraint("ck_payroll_employee_tax_calculation_known", type_="check")
        for column in (
            "tax_factor",
            "monthly_tax_addition",
            "monthly_tax_allowance",
            "unemployment_insurance_compulsory",
            "pension_insurance_compulsory",
            "care_insurance_children",
            "private_health_employer_subsidy",
            "private_health_premium",
            "private_health_insurance",
            "health_insurance_additional_rate",
            "church_denomination",
            "tax_calculation",
        ):
            batch.drop_column(column)
