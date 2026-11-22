"""Lohnbuchhaltung über die API."""

from __future__ import annotations

from datetime import date

from flask import current_app, jsonify, request
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
    UPDATABLE_EMPLOYEE_FIELDS,
    PayrollEmployeeInput,
    PayrollError,
    PayrollRunInput,
    calculate_wage_tax_preview,
    create_payroll_employee,
    create_payroll_run,
    decimal_from_payload,
    list_payroll_employees,
    list_payroll_runs,
    post_payroll_run,
    update_payroll_employee,
)
from app.services.payroll_pap import payroll_compliance_readiness
from app.services.wage_tax_returns import (
    WageTaxReturnError,
    compute_wage_tax_return,
    elster_period_code,
    list_wage_tax_returns,
    save_wage_tax_return,
    serialize_rows,
)
from domain.models import PayrollEmployee, PayrollRun, WageTaxReturn

_DATE_FIELDS = {"employment_start", "employment_end", "birth_date"}
_DECIMAL_FIELDS = {
    "child_allowances",
    "gross_monthly_salary",
    "wage_tax_rate",
    "church_tax_rate",
    "solidarity_surcharge_rate",
    "employee_social_security_rate",
    "employer_social_security_rate",
    "health_insurance_additional_rate",
    "private_health_premium",
    "private_health_employer_subsidy",
    "monthly_tax_allowance",
    "monthly_tax_addition",
    "tax_factor",
}
_BOOL_FIELDS = {
    "main_employment",
    "private_health_insurance",
    "pension_insurance_compulsory",
    "unemployment_insurance_compulsory",
}
_INT_FIELDS = {"tax_class", "care_insurance_children"}


def _payload_value(field: str, value: object) -> object:
    """JSON-Wert eines Mitarbeiterfelds in den Python-Typ des Service."""
    if value in (None, ""):
        if field in _BOOL_FIELDS:
            return False
        return None
    if field in _DATE_FIELDS:
        return date.fromisoformat(str(value))
    if field in _DECIMAL_FIELDS:
        return decimal_from_payload(value)
    if field in _BOOL_FIELDS:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "ja", "yes", "on"}
        return bool(value)
    if field in _INT_FIELDS:
        return int(value)  # type: ignore[call-overload]
    return value


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
        "birth_date": employee.birth_date.isoformat() if employee.birth_date else None,
        "tax_id": employee.tax_id,
        "tax_class": employee.tax_class,
        "child_allowances": str(employee.child_allowances),
        "federal_state": employee.federal_state,
        "main_employment": employee.main_employment,
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
        "cost_center_id": employee.cost_center_id,
        "profit_center_id": employee.profit_center_id,
        "tax_calculation": employee.tax_calculation,
        "church_denomination": employee.church_denomination,
        "health_insurance_additional_rate": str(employee.health_insurance_additional_rate),
        "private_health_insurance": employee.private_health_insurance,
        "private_health_premium": str(employee.private_health_premium),
        "private_health_employer_subsidy": str(employee.private_health_employer_subsidy),
        "care_insurance_children": employee.care_insurance_children,
        "pension_insurance_compulsory": employee.pension_insurance_compulsory,
        "unemployment_insurance_compulsory": employee.unemployment_insurance_compulsory,
        "monthly_tax_allowance": str(employee.monthly_tax_allowance),
        "monthly_tax_addition": str(employee.monthly_tax_addition),
        "tax_factor": str(employee.tax_factor) if employee.tax_factor is not None else None,
    }


def _wage_tax_return_dict(item: WageTaxReturn) -> dict[str, object]:
    return {
        "id": item.id,
        "company_id": item.company_id,
        "period_label": item.period_label,
        "elster_period": elster_period_code(item.period_label),
        "date_from": item.date_from.isoformat(),
        "date_to": item.date_to.isoformat(),
        "correction_number": item.correction_number,
        "is_correction": item.correction_number > 0,
        "employee_count": item.employee_count,
        "kennzahlen": item.kennzahlen,
        "payroll_run_ids": item.payroll_run_ids,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "created_by": item.created_by,
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
                "church_denomination": line.church_denomination,
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
            birth_date=(
                date.fromisoformat(payload["birth_date"]) if payload.get("birth_date") else None
            ),
            tax_id=payload.get("tax_id"),
            tax_class=int(payload.get("tax_class") or 1),
            child_allowances=decimal_from_payload(payload.get("child_allowances"), "0.0"),
            federal_state=payload.get("federal_state"),
            main_employment=bool(payload.get("main_employment", True)),
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
            cost_center_id=payload.get("cost_center_id"),
            profit_center_id=payload.get("profit_center_id"),
            tax_calculation=(payload.get("tax_calculation") or "manual"),
            church_denomination=payload.get("church_denomination"),
            health_insurance_additional_rate=decimal_from_payload(
                payload.get("health_insurance_additional_rate"), "2.90"
            ),
            private_health_insurance=bool(
                _payload_value("private_health_insurance", payload.get("private_health_insurance"))
            ),
            private_health_premium=decimal_from_payload(payload.get("private_health_premium")),
            private_health_employer_subsidy=decimal_from_payload(
                payload.get("private_health_employer_subsidy")
            ),
            care_insurance_children=int(payload.get("care_insurance_children") or 0),
            pension_insurance_compulsory=bool(payload.get("pension_insurance_compulsory", True)),
            unemployment_insurance_compulsory=bool(
                payload.get("unemployment_insurance_compulsory", True)
            ),
            monthly_tax_allowance=decimal_from_payload(payload.get("monthly_tax_allowance")),
            monthly_tax_addition=decimal_from_payload(payload.get("monthly_tax_addition")),
            tax_factor=(
                decimal_from_payload(payload["tax_factor"]) if payload.get("tax_factor") else None
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


@api_bp.get("/payroll/readiness")
def get_payroll_readiness_via_api():
    return jsonify(payroll_compliance_readiness(current_app.config)), 200


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
            config=current_app.config,
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


@api_bp.patch("/payroll/employees/<int:employee_id>")
def update_payroll_employee_via_api(employee_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    fields = {key: value for key, value in payload.items() if key != "employee_id"}
    unknown = sorted(set(fields) - set(UPDATABLE_EMPLOYEE_FIELDS))
    if unknown:
        return validation_error(f"Unbekannte Felder: {', '.join(unknown)}.")
    if not fields:
        return validation_error("Keine Änderungen übergeben.")
    try:
        changes = {key: _payload_value(key, value) for key, value in fields.items()}
    except (TypeError, ValueError):
        return jsonify({"error": "valid employee payroll payload is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        employee = session.get(PayrollEmployee, employee_id)
        if employee is None or api_scoped_company(session, employee.company_id) is None:
            return jsonify({"error": "Employee not found."}), 404
        try:
            updated = update_payroll_employee(
                session=session,
                employee_id=employee_id,
                changes=changes,
                changed_by=_api_changed_by(),
            )
        except PayrollError as exc:
            session.rollback()
            return validation_error(str(exc))
        return jsonify(_employee_dict(updated)), 200


@api_bp.post("/payroll/wage-tax/calculate")
def calculate_wage_tax_via_api():
    """Lohnsteuer eines Monatslohns nach dem amtlichen PAP – ohne Mitarbeiter anzulegen."""
    payload = request.get_json(silent=True) or {}
    try:
        values = {
            key: _payload_value(key, payload[key])
            for key in UPDATABLE_EMPLOYEE_FIELDS
            if key in payload and not key.endswith("_account_id")
        }
        period_label = str(payload.get("period_label") or "").strip()
    except (TypeError, ValueError):
        return jsonify({"error": "valid wage tax payload is required."}), 400
    try:
        result = calculate_wage_tax_preview(values=values, period_label=period_label)
    except PayrollError as exc:
        return validation_error(str(exc))
    return jsonify(result), 200


def _wage_tax_preview_dict(result) -> dict[str, object]:
    return {
        "company_id": result.company_id,
        "period_label": result.period_label,
        "elster_period": elster_period_code(result.period_label),
        "date_from": result.date_from.isoformat(),
        "date_to": result.date_to.isoformat(),
        "due_date": result.due_date.isoformat(),
        "employee_count": result.employee_count,
        "kennzahlen": serialize_rows(result.rows),
        "payroll_runs": result.payroll_runs,
        "draft_runs": result.draft_runs,
        "liability": result.liability,
        "issues": result.issues,
        "warnings": result.warnings,
    }


@api_bp.get("/payroll/wage-tax-returns/preview")
def preview_wage_tax_return_via_api():
    company_id = request.args.get("company_id", type=int)
    period_label = (request.args.get("period_label") or "").strip()
    if not company_id or not period_label:
        return jsonify({"error": "company_id and period_label are required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = compute_wage_tax_return(
                session=session, company_id=company_id, period_label=period_label
            )
        except WageTaxReturnError as exc:
            return validation_error(str(exc))
        return jsonify(_wage_tax_preview_dict(result)), 200


@api_bp.post("/payroll/wage-tax-returns")
def create_wage_tax_return_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400
    period_label = str(payload.get("period_label") or "").strip()
    if not period_label:
        return jsonify({"error": "period_label is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            item = save_wage_tax_return(
                session=session,
                company_id=company_id,
                period_label=period_label,
                changed_by=_api_changed_by(),
                correction=bool(payload.get("correction")),
            )
        except WageTaxReturnError as exc:
            session.rollback()
            return validation_error(str(exc))
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Wage tax return already exists for this period."}), 409
        return jsonify(_wage_tax_return_dict(item)), 201


@api_bp.get("/payroll/wage-tax-returns")
def list_wage_tax_returns_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        items = list_wage_tax_returns(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "wage_tax_returns": [_wage_tax_return_dict(item) for item in items],
                }
            ),
            200,
        )
