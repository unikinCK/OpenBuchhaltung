"""Lohnbuchhaltung UI."""

from __future__ import annotations

from datetime import date

from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.services.payroll import (
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
from app.services.payroll_pap import (
    CHURCH_DENOMINATIONS,
    FEDERAL_STATES,
    payroll_compliance_readiness,
)
from app.services.scoping import scoped_select
from app.services.wage_tax_returns import (
    WageTaxReturnError,
    compute_wage_tax_return,
    elster_period_code,
    list_wage_tax_returns,
    save_wage_tax_return,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, ControllingUnit, PayrollEmployee, PayrollRun


def _form_decimal(name: str, default: str = "0.00"):
    """Dezimalwert aus dem Formular; deutsches Komma ist erlaubt (``0,912``, ``1.234,56``)."""
    raw = (request.form.get(name) or "").strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return decimal_from_payload(raw, default)


def _form_flag(name: str, default: bool) -> bool:
    raw = request.form.get(name)
    if raw is None:
        return default
    return raw == "1"


def _form_date(name: str):
    raw = (request.form.get(name) or "").strip()
    return date.fromisoformat(raw) if raw else None


def _pap_form_values() -> dict[str, object]:
    factor = (request.form.get("tax_factor") or "").strip()
    return {
        "tax_calculation": request.form.get("tax_calculation", "manual").strip() or "manual",
        "church_denomination": request.form.get("church_denomination", "").strip() or None,
        "health_insurance_additional_rate": _form_decimal(
            "health_insurance_additional_rate", "2.90"
        ),
        "private_health_insurance": _form_flag("private_health_insurance", False),
        "private_health_premium": _form_decimal("private_health_premium"),
        "private_health_employer_subsidy": _form_decimal("private_health_employer_subsidy"),
        "care_insurance_children": int(request.form.get("care_insurance_children") or 0),
        "pension_insurance_compulsory": _form_flag("pension_insurance_compulsory", True),
        "unemployment_insurance_compulsory": _form_flag(
            "unemployment_insurance_compulsory", True
        ),
        "monthly_tax_allowance": _form_decimal("monthly_tax_allowance"),
        "monthly_tax_addition": _form_decimal("monthly_tax_addition"),
        "tax_factor": _form_decimal("tax_factor") if factor else None,
    }


def _period_options(today: date) -> list[str]:
    options = []
    for year in (today.year, today.year - 1):
        options.extend(f"{year}-{month:02d}" for month in range(1, 13))
        options.extend(f"{year}-Q{quarter}" for quarter in range(1, 5))
        options.append(str(year))
    return options


@main_bp.get("/lohn")
def payroll_page():
    return _render_payroll_page()


def _render_payroll_page(
    *, calc_result: dict | None = None, calc_values: dict | None = None, calc_error=None
):
    session_factory = get_session_factory()
    lsta_period = (request.args.get("lsta_period") or "").strip()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        if request.method == "POST":
            selected_company_id = request.form.get("company_id", type=int) or selected_company_id
        accounts = []
        employees = []
        runs = []
        cost_centers = []
        profit_centers = []
        wage_tax_returns = []
        lsta_preview = None
        lsta_error = None
        if selected_company_id:
            wage_tax_returns = list_wage_tax_returns(
                session=session, company_id=selected_company_id
            )
            if lsta_period:
                try:
                    lsta_preview = compute_wage_tax_return(
                        session=session,
                        company_id=selected_company_id,
                        period_label=lsta_period,
                    )
                except WageTaxReturnError as exc:
                    lsta_error = str(exc)
            accounts = (
                session.execute(
                    scoped_select(Account, company_id=selected_company_id)
                    .where(Account.is_active.is_(True))
                    .order_by(Account.code)
                )
                .scalars()
                .all()
            )
            employees = list_payroll_employees(
                session=session, company_id=selected_company_id
            )
            runs = list_payroll_runs(session=session, company_id=selected_company_id)
            controlling_units = (
                session.execute(
                    scoped_select(ControllingUnit, company_id=selected_company_id)
                    .where(ControllingUnit.is_active.is_(True))
                    .order_by(ControllingUnit.code)
                )
                .scalars()
                .all()
            )
            cost_centers = [u for u in controlling_units if u.unit_type == "cost_center"]
            profit_centers = [u for u in controlling_units if u.unit_type == "profit_center"]

    today = date.today()
    return render_template(
        "lohn.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        employees=employees,
        runs=runs,
        cost_centers=cost_centers,
        profit_centers=profit_centers,
        payroll_readiness=payroll_compliance_readiness(current_app.config),
        default_period=f"{today.year}-{today.month:02d}",
        today=today.isoformat(),
        church_denominations=CHURCH_DENOMINATIONS,
        federal_states=FEDERAL_STATES,
        wage_tax_returns=wage_tax_returns,
        lsta_period=lsta_period,
        lsta_preview=lsta_preview,
        lsta_error=lsta_error,
        lsta_period_options=_period_options(today),
        elster_period_code=elster_period_code,
        calc_result=calc_result,
        calc_values=calc_values or {},
        calc_error=calc_error,
        employee=None,
    )


@main_bp.post("/lohn/mitarbeiter")
def create_payroll_employee_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        flash("Gesellschaft ist Pflicht.", "error")
        return redirect(url_for("main.payroll_page"))

    try:
        employee_input = PayrollEmployeeInput(
            company_id=company_id,
            employee_number=request.form.get("employee_number", "").strip(),
            first_name=request.form.get("first_name", "").strip(),
            last_name=request.form.get("last_name", "").strip(),
            employment_start=date.fromisoformat(
                request.form.get("employment_start", "").strip()
            ),
            gross_monthly_salary=_form_decimal("gross_monthly_salary"),
            birth_date=(
                date.fromisoformat(request.form.get("birth_date", "").strip())
                if request.form.get("birth_date", "").strip()
                else None
            ),
            tax_class=int(request.form.get("tax_class", "1")),
            child_allowances=_form_decimal("child_allowances", "0.0"),
            federal_state=request.form.get("federal_state", "").strip() or None,
            main_employment=request.form.get("main_employment", "1") == "1",
            wage_tax_rate=_form_decimal("wage_tax_rate"),
            church_tax_rate=_form_decimal("church_tax_rate"),
            solidarity_surcharge_rate=_form_decimal("solidarity_surcharge_rate"),
            employee_social_security_rate=_form_decimal("employee_social_security_rate"),
            employer_social_security_rate=_form_decimal("employer_social_security_rate"),
            wage_expense_account_id=request.form.get("wage_expense_account_id", type=int),
            employer_social_security_expense_account_id=request.form.get(
                "employer_social_security_expense_account_id", type=int
            ),
            payroll_liability_account_id=request.form.get(
                "payroll_liability_account_id", type=int
            ),
            wage_tax_liability_account_id=request.form.get(
                "wage_tax_liability_account_id", type=int
            ),
            social_security_liability_account_id=request.form.get(
                "social_security_liability_account_id", type=int
            ),
            tax_id=request.form.get("tax_id", "").strip() or None,
            social_security_number=request.form.get(
                "social_security_number", ""
            ).strip()
            or None,
            cost_center_id=request.form.get("cost_center_id", type=int),
            profit_center_id=request.form.get("profit_center_id", type=int),
            changed_by=changed_by(),
            **_pap_form_values(),
        )
    except ValueError:
        flash("Mitarbeiterdaten enthalten ungültige Datums-, Betrags- oder Ratenwerte.", "error")
        return redirect(url_for("main.payroll_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            employee = create_payroll_employee(session=session, payload=employee_input)
        except PayrollError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.payroll_page", company_id=company_id))
        except IntegrityError:
            session.rollback()
            flash("Diese Personalnummer existiert bereits.", "error")
            return redirect(url_for("main.payroll_page", company_id=company_id))

    flash(f"Mitarbeiter {employee.employee_number} wurde angelegt.", "success")
    return redirect(url_for("main.payroll_page", company_id=company_id))


@main_bp.post("/lohn/laeufe")
def create_payroll_run_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        flash("Gesellschaft ist Pflicht.", "error")
        return redirect(url_for("main.payroll_page"))

    try:
        run_input = PayrollRunInput(
            company_id=company_id,
            period_label=request.form.get("period_label", "").strip(),
            payment_date=date.fromisoformat(request.form.get("payment_date", "").strip()),
            changed_by=changed_by(),
            config=current_app.config,
        )
    except ValueError:
        flash("Zeitraum oder Zahlungsdatum ist ungültig.", "error")
        return redirect(url_for("main.payroll_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            run = create_payroll_run(session=session, payload=run_input)
        except PayrollError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.payroll_page", company_id=company_id))
        except IntegrityError:
            session.rollback()
            flash("Für diesen Zeitraum existiert bereits ein Lohnlauf.", "error")
            return redirect(url_for("main.payroll_page", company_id=company_id))

    flash(f"Lohnlauf {run.period_label} wurde als Entwurf erstellt.", "success")
    return redirect(url_for("main.payroll_page", company_id=company_id))


@main_bp.post("/lohn/laeufe/<int:payroll_run_id>/buchen")
def post_payroll_run_action(payroll_run_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        run = session.get(PayrollRun, payroll_run_id)
        if run is None:
            flash("Lohnlauf wurde nicht gefunden.", "error")
            return redirect(url_for("main.payroll_page"))
        require_company_access(session, run.company_id)
        company_id = run.company_id
        try:
            posted = post_payroll_run(
                session=session,
                payroll_run_id=payroll_run_id,
                changed_by=changed_by(),
            )
        except PayrollError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.payroll_page", company_id=company_id))

    flash(
        f"Lohnlauf {posted.period_label} wurde als Buchung #{posted.journal_entry_id} gebucht.",
        "success",
    )
    return redirect(url_for("main.payroll_page", company_id=company_id))


@main_bp.get("/lohn/mitarbeiter/<int:employee_id>")
def payroll_employee_page(employee_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        employee = session.get(PayrollEmployee, employee_id)
        if employee is None:
            flash("Mitarbeiter wurde nicht gefunden.", "error")
            return redirect(url_for("main.payroll_page"))
        require_company_access(session, employee.company_id)
        accounts = (
            session.execute(
                scoped_select(Account, company_id=employee.company_id)
                .where(Account.is_active.is_(True))
                .order_by(Account.code)
            )
            .scalars()
            .all()
        )
        controlling_units = (
            session.execute(
                scoped_select(ControllingUnit, company_id=employee.company_id)
                .where(ControllingUnit.is_active.is_(True))
                .order_by(ControllingUnit.code)
            )
            .scalars()
            .all()
        )
        session.expunge_all()
    return render_template(
        "lohn_mitarbeiter.html",
        employee=employee,
        accounts=accounts,
        cost_centers=[u for u in controlling_units if u.unit_type == "cost_center"],
        profit_centers=[u for u in controlling_units if u.unit_type == "profit_center"],
        church_denominations=CHURCH_DENOMINATIONS,
        federal_states=FEDERAL_STATES,
        selected_company_id=employee.company_id,
    )


@main_bp.post("/lohn/mitarbeiter/<int:employee_id>")
def update_payroll_employee_action(employee_id: int):
    try:
        changes: dict[str, object] = {
            "first_name": request.form.get("first_name", "").strip(),
            "last_name": request.form.get("last_name", "").strip(),
            "employment_start": _form_date("employment_start"),
            "employment_end": _form_date("employment_end"),
            "status": request.form.get("status", "active"),
            "birth_date": _form_date("birth_date"),
            "tax_id": request.form.get("tax_id"),
            "social_security_number": request.form.get("social_security_number"),
            "tax_class": int(request.form.get("tax_class", "1")),
            "child_allowances": _form_decimal("child_allowances", "0.0"),
            "federal_state": request.form.get("federal_state", "").strip() or None,
            "main_employment": _form_flag("main_employment", True),
            "gross_monthly_salary": _form_decimal("gross_monthly_salary"),
            "wage_tax_rate": _form_decimal("wage_tax_rate"),
            "church_tax_rate": _form_decimal("church_tax_rate"),
            "solidarity_surcharge_rate": _form_decimal("solidarity_surcharge_rate"),
            "employee_social_security_rate": _form_decimal("employee_social_security_rate"),
            "employer_social_security_rate": _form_decimal("employer_social_security_rate"),
            "cost_center_id": request.form.get("cost_center_id", type=int),
            "profit_center_id": request.form.get("profit_center_id", type=int),
            **_pap_form_values(),
        }
        for field in (
            "wage_expense_account_id",
            "employer_social_security_expense_account_id",
            "payroll_liability_account_id",
            "wage_tax_liability_account_id",
            "social_security_liability_account_id",
        ):
            if request.form.get(field):
                changes[field] = request.form.get(field, type=int)
    except ValueError:
        flash("Mitarbeiterdaten enthalten ungültige Datums-, Betrags- oder Ratenwerte.", "error")
        return redirect(url_for("main.payroll_employee_page", employee_id=employee_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        employee = session.get(PayrollEmployee, employee_id)
        if employee is None:
            flash("Mitarbeiter wurde nicht gefunden.", "error")
            return redirect(url_for("main.payroll_page"))
        require_company_access(session, employee.company_id)
        company_id = employee.company_id
        try:
            updated = update_payroll_employee(
                session=session,
                employee_id=employee_id,
                changes=changes,
                changed_by=changed_by(),
            )
        except PayrollError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.payroll_employee_page", employee_id=employee_id))
        number = updated.employee_number

    flash(f"Mitarbeiter {number} wurde gespeichert.", "success")
    return redirect(url_for("main.payroll_page", company_id=company_id))


@main_bp.post("/lohn/lohnsteuer-rechner")
def calculate_wage_tax_action():
    values: dict[str, object] = {}
    error = None
    try:
        values = {
            "gross_monthly_salary": _form_decimal("gross_monthly_salary"),
            "tax_class": int(request.form.get("tax_class", "1")),
            "child_allowances": _form_decimal("child_allowances", "0.0"),
            "birth_date": _form_date("birth_date"),
            "federal_state": request.form.get("federal_state", "").strip() or None,
            **_pap_form_values(),
        }
        values.pop("tax_calculation", None)
    except ValueError:
        error = "Eingaben enthalten ungültige Datums- oder Betragswerte."
    period_label = request.form.get("period_label", "").strip()
    result = None
    if error is None:
        try:
            result = calculate_wage_tax_preview(values=values, period_label=period_label)
        except PayrollError as exc:
            error = str(exc)
    values["period_label"] = period_label
    return _render_payroll_page(calc_result=result, calc_values=values, calc_error=error)


@main_bp.post("/lohn/lohnsteuer-anmeldung")
def create_wage_tax_return_action():
    company_id = request.form.get("company_id", type=int)
    period_label = request.form.get("period_label", "").strip()
    if not company_id:
        flash("Gesellschaft ist Pflicht.", "error")
        return redirect(url_for("main.payroll_page"))
    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            item = save_wage_tax_return(
                session=session,
                company_id=company_id,
                period_label=period_label,
                changed_by=changed_by(),
                correction=request.form.get("correction") == "1",
            )
        except WageTaxReturnError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(
                url_for("main.payroll_page", company_id=company_id, lsta_period=period_label)
                + "#lohnsteuer-anmeldung"
            )
        label = item.period_label
        correction = item.correction_number
    suffix = f" (Berichtigung {correction})" if correction else ""
    flash(f"Lohnsteuer-Anmeldung {label}{suffix} wurde festgehalten.", "success")
    return redirect(
        url_for("main.payroll_page", company_id=company_id, lsta_period=label)
        + "#lohnsteuer-anmeldung"
    )
