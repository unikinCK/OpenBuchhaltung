from __future__ import annotations

import json
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.audit_export import build_audit_export_package
from app.services.payroll import (
    PayrollEmployeeInput,
    PayrollRunInput,
    create_payroll_employee,
    create_payroll_run,
    post_payroll_run,
)
from domain.models import (
    Account,
    AuditLog,
    Base,
    Company,
    JournalEntry,
    JournalEntryLine,
    PayrollRun,
    Tenant,
    User,
)


def _accounts(session: Session, company: Company) -> dict[str, Account]:
    specs = {
        "4120": ("Loehne und Gehaelter", "expense"),
        "4130": ("Gesetzliche soziale Aufwendungen", "expense"),
        "1740": ("Verbindlichkeiten Lohn und Gehalt", "liability"),
        "1741": ("Verbindlichkeiten Lohnsteuer", "liability"),
        "1742": ("Verbindlichkeiten Sozialversicherung", "liability"),
    }
    accounts = {
        code: Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
        )
        for code, (name, account_type) in specs.items()
    }
    session.add_all(accounts.values())
    session.flush()
    return accounts


def _seed(session: Session) -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name="Lohn Tenant")
    company = Company(tenant=tenant, name="Lohn GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts = _accounts(session, company)
    session.commit()
    return company, accounts


def _employee_input(company: Company) -> PayrollEmployeeInput:
    return PayrollEmployeeInput(
        company_id=company.id,
        employee_number="P-001",
        first_name="Ada",
        last_name="Lovelace",
        employment_start=date(2026, 1, 1),
        gross_monthly_salary=Decimal("4000.00"),
        wage_tax_rate=Decimal("0.20"),
        church_tax_rate=Decimal("0.01"),
        employee_social_security_rate=Decimal("0.20"),
        employer_social_security_rate=Decimal("0.20"),
        wage_expense_account_code="4120",
        employer_social_security_expense_account_code="4130",
        payroll_liability_account_code="1740",
        wage_tax_liability_account_code="1741",
        social_security_liability_account_code="1742",
        changed_by="pytest",
    )


def test_payroll_run_posts_balanced_journal_entry() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        company, accounts = _seed(session)
        create_payroll_employee(session=session, payload=_employee_input(company))
        run = create_payroll_run(
            session=session,
            payload=PayrollRunInput(
                company_id=company.id,
                period_label="2026-05",
                payment_date=date(2026, 5, 31),
                changed_by="pytest",
            ),
        )

        assert run.gross_total == Decimal("4000.00")
        assert run.wage_tax_total == Decimal("800.00")
        assert run.church_tax_total == Decimal("40.00")
        assert run.employee_social_security_total == Decimal("800.00")
        assert run.employer_social_security_total == Decimal("800.00")
        assert run.net_total == Decimal("2360.00")

        posted = post_payroll_run(
            session=session, payroll_run_id=run.id, changed_by="pytest"
        )
        assert posted.status == "posted"
        assert posted.journal_entry_id is not None

        journal = session.get(JournalEntry, posted.journal_entry_id)
        assert journal is not None
        assert journal.source == "payroll"
        lines = session.execute(
            select(JournalEntryLine).where(
                JournalEntryLine.journal_entry_id == posted.journal_entry_id
            )
        ).scalars().all()
        debit = {line.account_id: line.debit_amount for line in lines if line.debit_amount}
        credit = {
            line.account_id: line.credit_amount for line in lines if line.credit_amount
        }
        assert debit == {
            accounts["4120"].id: Decimal("4000.00"),
            accounts["4130"].id: Decimal("800.00"),
        }
        assert credit == {
            accounts["1740"].id: Decimal("2360.00"),
            accounts["1741"].id: Decimal("840.00"),
            accounts["1742"].id: Decimal("1600.00"),
        }

        actions = session.execute(
            select(AuditLog.action).where(AuditLog.entity_type == "payroll_run")
        ).scalars().all()
        assert actions == ["created", "posted"]


def _create_test_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'payroll.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as session:
        company, accounts = _seed(session)
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.commit()
        return app, company.id, {code: account.id for code, account in accounts.items()}


def test_payroll_api_create_employee_and_auto_post_run(tmp_path: Path) -> None:
    app, company_id, _ = _create_test_app(tmp_path)
    client = app.test_client()

    employee = client.post(
        "/api/v1/payroll/employees",
        json={
            "company_id": company_id,
            "employee_number": "P-001",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "employment_start": "2026-01-01",
            "gross_monthly_salary": "4000.00",
            "wage_tax_rate": "0.20",
            "church_tax_rate": "0.01",
            "employee_social_security_rate": "0.20",
            "employer_social_security_rate": "0.20",
            "wage_expense_account_code": "4120",
            "employer_social_security_expense_account_code": "4130",
            "payroll_liability_account_code": "1740",
            "wage_tax_liability_account_code": "1741",
            "social_security_liability_account_code": "1742",
        },
    )
    assert employee.status_code == 201

    run_response = client.post(
        "/api/v1/payroll/runs",
        json={
            "company_id": company_id,
            "period_label": "2026-05",
            "payment_date": "2026-05-31",
            "auto_post": True,
        },
    )
    assert run_response.status_code == 201
    run = run_response.get_json()
    assert run["status"] == "posted"
    assert run["gross_total"] == "4000.00"
    assert run["net_total"] == "2360.00"
    assert run["journal_entry_id"] is not None

    listing = client.get("/api/v1/payroll/runs", query_string={"company_id": company_id})
    assert listing.status_code == 200
    assert listing.get_json()["runs"][0]["period_label"] == "2026-05"


def test_payroll_ui_and_audit_export_include_runs(tmp_path: Path) -> None:
    app, company_id, account_ids = _create_test_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    page = client.get("/lohn", query_string={"company_id": company_id})
    assert page.status_code == 200
    assert "Lohnbuchhaltung" in page.get_data(as_text=True)

    response = client.post(
        "/lohn/mitarbeiter",
        data={
            "company_id": str(company_id),
            "employee_number": "P-001",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "employment_start": "2026-01-01",
            "gross_monthly_salary": "4000.00",
            "wage_tax_rate": "0.20",
            "church_tax_rate": "0.01",
            "employee_social_security_rate": "0.20",
            "employer_social_security_rate": "0.20",
            "wage_expense_account_id": str(account_ids["4120"]),
            "employer_social_security_expense_account_id": str(account_ids["4130"]),
            "payroll_liability_account_id": str(account_ids["1740"]),
            "wage_tax_liability_account_id": str(account_ids["1741"]),
            "social_security_liability_account_id": str(account_ids["1742"]),
        },
    )
    assert response.status_code == 302

    run_response = client.post(
        "/lohn/laeufe",
        data={
            "company_id": str(company_id),
            "period_label": "2026-05",
            "payment_date": "2026-05-31",
        },
    )
    assert run_response.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        payroll_run = session.execute(select(PayrollRun)).scalar_one()
        run_id = payroll_run.id

    post_response = client.post(f"/lohn/laeufe/{run_id}/buchen")
    assert post_response.status_code == 302

    with app.extensions["db_session_factory"]() as session:
        package = build_audit_export_package(
            session=session,
            company_id=company_id,
            generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            include_documents=False,
        )
    with zipfile.ZipFile(BytesIO(package.payload)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["table_counts"]["payroll_employees"] == 1
        assert manifest["table_counts"]["payroll_runs"] == 1
        assert manifest["table_counts"]["payroll_run_lines"] == 1
