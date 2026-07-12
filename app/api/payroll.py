"""Lohnbuchhaltung über die API."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_user
from app.services.payroll import (
    PayrollEmployeeInput,
    PayrollError,
    PayrollRunInput,
    create_payroll_employee,
    create_payroll_run,
    decimal_from_payload,
    list_payroll_employees,
    list_payroll_runs,
    post_payroll_run,
)
from domain.models import PayrollRun


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _employee_dict(employee) -> dict[str, object]:
    return {
        "id": employee.id,
        "company_id": employee.company_id,
        "employee_number": employee.employee_number,
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "employment_start": employee.employment_start.isoformat(),
        "employment_end": employee.employment_end.isoformat()
        if employee.employment_end
        else None,
        "status": employee.status,
        "tax_id": employee.tax_id,
        "social_security_number": employee.social_security_number,
        "gross_monthly_salary": str(employee.gross_monthly_salary),
        "wage_tax_rate": str(employee.wage_tax_rate),
        "church_tax_rate": str(employee.church_tax_rate),
        "solidarity_surcharge_rate": str(employee.solidarity_surcharge_rate),
        "employee_social_security_rate": str(employee.employee_social_security_rate),
        "employer_social_security_rate": str(employee.employer_social_security_rate),
        "wage_expense_account_id": employee.wage_expense_account_id,
        "employer_social_security_expense_account_id": (
            employee.employer_social_security_expense_account_id
        ),
        "payroll_liability_account_id": employee.payroll_liability_account_id,
        "wage_tax_liability_account_id": employee.wage_tax_liability_account_id,
        "social_security_liability_account_id": (
            employee.social_security_liability_account_id
        ),
    }


def _run_dict(run: PayrollRun) -> dict[str, object]:
    return {
        "id": run.id,
        "company_id": run.company_id,
        "period_label": run.period_label,
        "payment_date": run.payment_date.isoformat(),
        "status": run.status,
        "journal_entry_id": run.journal_entry_id,
        "gross_total": str(run.gross_total),
        "wage_tax_total": str(run.wage_tax_total),
        "church_tax_total": str(run.church_tax_total),
        "solidarity_surcharge_total": str(run.solidarity_surcharge_total),
        "employee_social_security_total": str(run.employee_social_security_total),
        "employer_social_security_total": str(run.employer_social_security_total),
        "net_total": str(run.net_total),
        "created_at": run.created_at.isoformat(),
        "created_by": run.created_by,
        "posted_at": run.posted_at.isoformat() if run.posted_at else None,
        "lines": [
            {
                "id": line.id,
                "employee_id": line.employee_id,
                "employee_number": line.employee.employee_number,
                "employee_name": f"{line.employee.first_name} {line.employee.last_name}",
                "gross_pay": str(line.gross_pay),
                "wage_tax": str(line.wage_tax),
                "church_tax": str(line.church_tax),
                "solidarity_surcharge": str(line.solidarity_surcharge),
                "employee_social_security": str(line.employee_social_security),
                "employer_social_security": str(line.employer_social_security),
                "net_pay": str(line.net_pay),
                "employer_total": str(line.employer_total),
                "calculation": line.calculation,
            }
            for line in run.lines
        ],
    }


@api_bp.post("/payroll/employees")
def create_payroll_employee_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        employee_input = PayrollEmployeeInput(
            company_id=company_id,
            employee_number=(payload.get("employee_number") or "").strip(),
            first_name=(payload.get("first_name") or "").strip(),
            last_name=(payload.get("last_name") or "").strip(),
            employment_start=date.fromisoformat(payload.get("employment_start")),
            employment_end=(
                date.fromisoformat(payload["employment_end"])
                if payload.get("employment_end")
                else None
            ),
            status=(payload.get("status") or "active").strip(),
            tax_id=payload.get("tax_id"),
            social_security_number=payload.get("social_security_number"),
            gross_monthly_salary=decimal_from_payload(payload.get("gross_monthly_salary")),
            wage_tax_rate=decimal_from_payload(payload.get("wage_tax_rate")),
            church_tax_rate=decimal_from_payload(payload.get("church_tax_rate")),
            solidarity_surcharge_rate=decimal_from_payload(
                payload.get("solidarity_surcharge_rate")
            ),
            employee_social_security_rate=decimal_from_payload(
                payload.get("employee_social_security_rate")
            ),
            employer_social_security_rate=decimal_from_payload(
                payload.get("employer_social_security_rate")
            ),
            wage_expense_account_id=payload.get("wage_expense_account_id"),
            wage_expense_account_code=payload.get("wage_expense_account_code"),
            employer_social_security_expense_account_id=payload.get(
                "employer_social_security_expense_account_id"
            ),
            employer_social_security_expense_account_code=payload.get(
                "employer_social_security_expense_account_code"
            ),
            payroll_liability_account_id=payload.get("payroll_liability_account_id"),
            payroll_liability_account_code=payload.get("payroll_liability_account_code"),
            wage_tax_liability_account_id=payload.get("wage_tax_liability_account_id"),
            wage_tax_liability_account_code=payload.get("wage_tax_liability_account_code"),
            social_security_liability_account_id=payload.get(
                "social_security_liability_account_id"
            ),
            social_security_liability_account_code=payload.get(
                "social_security_liability_account_code"
            ),
            changed_by=_api_changed_by(),
        )
    except (TypeError, ValueError):
        return jsonify({"error": "valid employee payroll payload is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            employee = create_payroll_employee(session=session, payload=employee_input)
        except PayrollError as exc:
            return validation_error(str(exc))
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Employee number already exists."}), 409
        return jsonify(_employee_dict(employee)), 201


@api_bp.get("/payroll/employees")
def list_payroll_employees_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        employees = list_payroll_employees(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "employees": [_employee_dict(employee) for employee in employees],
                }
            ),
            200,
        )


@api_bp.post("/payroll/runs")
def create_payroll_run_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        employee_ids = payload.get("employee_ids")
        run_input = PayrollRunInput(
            company_id=company_id,
            period_label=(payload.get("period_label") or "").strip(),
            payment_date=date.fromisoformat(payload.get("payment_date")),
            employee_ids=[int(item) for item in employee_ids] if employee_ids else None,
            changed_by=_api_changed_by(),
        )
    except (TypeError, ValueError):
        return jsonify({"error": "valid payroll run payload is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            run = create_payroll_run(session=session, payload=run_input)
            if payload.get("auto_post"):
                run = post_payroll_run(
                    session=session, payroll_run_id=run.id, changed_by=_api_changed_by()
                )
        except PayrollError as exc:
            return validation_error(str(exc))
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Payroll run already exists for this period."}), 409
        return jsonify(_run_dict(run)), 201


@api_bp.get("/payroll/runs")
def list_payroll_runs_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        runs = list_payroll_runs(session=session, company_id=company_id)
        return jsonify({"company_id": company_id, "runs": [_run_dict(run) for run in runs]}), 200


@api_bp.post("/payroll/runs/<int:payroll_run_id>/post")
def post_payroll_run_via_api(payroll_run_id: int):
    if not api_can_write():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        run = session.get(PayrollRun, payroll_run_id)
        if run is None or api_scoped_company(session, run.company_id) is None:
            return jsonify({"error": "Payroll run not found."}), 404
        try:
            posted = post_payroll_run(
                session=session,
                payroll_run_id=payroll_run_id,
                changed_by=_api_changed_by(),
            )
        except PayrollError as exc:
            return validation_error(str(exc))
        return jsonify(_run_dict(posted)), 200
