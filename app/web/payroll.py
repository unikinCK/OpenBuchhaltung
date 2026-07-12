"""Lohnbuchhaltung UI."""

from __future__ import annotations

from datetime import date

from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

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
from app.services.payroll_pap import payroll_compliance_readiness
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, PayrollRun


@main_bp.get("/lohn")
def payroll_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        accounts = []
        employees = []
        runs = []
        if selected_company_id:
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

    today = date.today()
    return render_template(
        "lohn.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        employees=employees,
        runs=runs,
        payroll_readiness=payroll_compliance_readiness(current_app.config),
        default_period=f"{today.year}-{today.month:02d}",
        today=today.isoformat(),
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
            gross_monthly_salary=decimal_from_payload(
                request.form.get("gross_monthly_salary")
            ),
            birth_date=(
                date.fromisoformat(request.form.get("birth_date", "").strip())
                if request.form.get("birth_date", "").strip()
                else None
            ),
            tax_class=int(request.form.get("tax_class", "1")),
            child_allowances=decimal_from_payload(
                request.form.get("child_allowances"), "0.0"
            ),
            federal_state=request.form.get("federal_state", "").strip() or None,
            main_employment=request.form.get("main_employment", "1") == "1",
            wage_tax_rate=decimal_from_payload(request.form.get("wage_tax_rate")),
            church_tax_rate=decimal_from_payload(request.form.get("church_tax_rate")),
            solidarity_surcharge_rate=decimal_from_payload(
                request.form.get("solidarity_surcharge_rate")
            ),
            employee_social_security_rate=decimal_from_payload(
                request.form.get("employee_social_security_rate")
            ),
            employer_social_security_rate=decimal_from_payload(
                request.form.get("employer_social_security_rate")
            ),
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
            changed_by=changed_by(),
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
