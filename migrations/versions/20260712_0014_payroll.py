"""Lohnbuchhaltung MVP.

Revision ID: 20260712_0014
Revises: 20260712_0013
Create Date: 2026-07-12 23:55:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260712_0014"
down_revision = "20260712_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_employee",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("employee_number", sa.String(length=40), nullable=False),
        sa.Column("first_name", sa.String(length=120), nullable=False),
        sa.Column("last_name", sa.String(length=120), nullable=False),
        sa.Column("employment_start", sa.Date(), nullable=False),
        sa.Column("employment_end", sa.Date()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("tax_id", sa.String(length=40)),
        sa.Column("social_security_number", sa.String(length=40)),
        sa.Column("gross_monthly_salary", sa.Numeric(14, 2), nullable=False),
        sa.Column("wage_tax_rate", sa.Numeric(7, 4), nullable=False, server_default="0"),
        sa.Column("church_tax_rate", sa.Numeric(7, 4), nullable=False, server_default="0"),
        sa.Column(
            "solidarity_surcharge_rate", sa.Numeric(7, 4), nullable=False, server_default="0"
        ),
        sa.Column(
            "employee_social_security_rate",
            sa.Numeric(7, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "employer_social_security_rate",
            sa.Numeric(7, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("wage_expense_account_id", sa.Integer(), nullable=False),
        sa.Column("employer_social_security_expense_account_id", sa.Integer(), nullable=False),
        sa.Column("payroll_liability_account_id", sa.Integer(), nullable=False),
        sa.Column("wage_tax_liability_account_id", sa.Integer(), nullable=False),
        sa.Column("social_security_liability_account_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["wage_expense_account_id"], ["account.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["employer_social_security_expense_account_id"],
            ["account.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payroll_liability_account_id"], ["account.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["wage_tax_liability_account_id"], ["account.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["social_security_liability_account_id"], ["account.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "company_id", "employee_number", name="uq_payroll_employee_number"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_payroll_employee_status_known"
        ),
        sa.CheckConstraint(
            "gross_monthly_salary >= 0", name="ck_payroll_employee_salary_nonneg"
        ),
        sa.CheckConstraint(
            "wage_tax_rate >= 0", name="ck_payroll_employee_wage_tax_nonneg"
        ),
        sa.CheckConstraint(
            "employee_social_security_rate >= 0",
            name="ck_payroll_employee_employee_sv_nonneg",
        ),
        sa.CheckConstraint(
            "employer_social_security_rate >= 0",
            name="ck_payroll_employee_employer_sv_nonneg",
        ),
    )
    op.create_index(
        "ix_payroll_employee_company_status",
        "payroll_employee",
        ["company_id", "status", "last_name"],
    )

    op.create_table(
        "payroll_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer()),
        sa.Column("period_label", sa.String(length=10), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("gross_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("wage_tax_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("church_tax_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column(
            "solidarity_surcharge_total",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "employee_social_security_total",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "employer_social_security_total",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("net_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("company_id", "period_label", name="uq_payroll_run_company_period"),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_payroll_run_status_known"),
        sa.CheckConstraint("gross_total >= 0", name="ck_payroll_run_gross_nonneg"),
        sa.CheckConstraint("net_total >= 0", name="ck_payroll_run_net_nonneg"),
    )
    op.create_index(
        "ix_payroll_run_company_period", "payroll_run", ["company_id", "period_label"]
    )

    op.create_table(
        "payroll_run_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("payroll_run_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("gross_pay", sa.Numeric(14, 2), nullable=False),
        sa.Column("wage_tax", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("church_tax", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column(
            "solidarity_surcharge", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "employee_social_security",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "employer_social_security",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("net_pay", sa.Numeric(14, 2), nullable=False),
        sa.Column("employer_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["company.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payroll_run_id"], ["payroll_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["payroll_employee.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "payroll_run_id", "employee_id", name="uq_payroll_line_employee"
        ),
        sa.CheckConstraint("gross_pay >= 0", name="ck_payroll_line_gross_nonneg"),
        sa.CheckConstraint("net_pay >= 0", name="ck_payroll_line_net_nonneg"),
    )
    op.create_index(
        "ix_payroll_line_company_run", "payroll_run_line", ["company_id", "payroll_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_line_company_run", table_name="payroll_run_line")
    op.drop_table("payroll_run_line")
    op.drop_index("ix_payroll_run_company_period", table_name="payroll_run")
    op.drop_table("payroll_run")
    op.drop_index("ix_payroll_employee_company_status", table_name="payroll_employee")
    op.drop_table("payroll_employee")
