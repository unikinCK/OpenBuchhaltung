"""Perioden und Geschäftsjahre: Sperren, Entsperren, Jahresabschluss."""

from __future__ import annotations

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.auth import ROLE_ADMIN, current_user
from app.services.periods import (
    PeriodActionError,
    close_fiscal_year,
    create_fiscal_year,
    lock_period,
    set_fiscal_year_start_month,
    unlock_period,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    MONTH_NAMES,
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
    require_period_access,
)
from domain.models import FiscalYear, Period, PeriodLock


@main_bp.get("/perioden")
def periods_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        fiscal_years = []
        periods_by_year: dict[int, list[Period]] = {}
        locked_period_ids: set[int] = set()
        if selected_company_id:
            fiscal_years = (
                session.execute(
                    select(FiscalYear)
                    .where(FiscalYear.company_id == selected_company_id)
                    .order_by(FiscalYear.label.desc())
                )
                .scalars()
                .all()
            )
            year_ids = [fiscal_year.id for fiscal_year in fiscal_years]
            if year_ids:
                periods = (
                    session.execute(
                        select(Period)
                        .where(Period.fiscal_year_id.in_(year_ids))
                        .order_by(Period.period_number)
                    )
                    .scalars()
                    .all()
                )
                for period in periods:
                    periods_by_year.setdefault(period.fiscal_year_id, []).append(period)
                locked_period_ids = set(
                    session.execute(
                        select(PeriodLock.period_id).where(
                            PeriodLock.period_id.in_([period.id for period in periods])
                        )
                    ).scalars()
                )

        selected_company = next(
            (company for company in companies if company.id == selected_company_id), None
        )
        fiscal_year_start_month = (
            selected_company.fiscal_year_start_month if selected_company else 1
        )

    user = current_user()
    return render_template(
        "perioden.html",
        companies=companies,
        selected_company_id=selected_company_id,
        fiscal_years=fiscal_years,
        periods_by_year=periods_by_year,
        locked_period_ids=locked_period_ids,
        fiscal_year_start_month=fiscal_year_start_month,
        month_names=MONTH_NAMES,
        is_admin=user is not None and user["role"] == ROLE_ADMIN,
    )


@main_bp.post("/perioden/<int:period_id>/sperren")
def lock_period_action(period_id: int):
    company_id = request.form.get("company_id", type=int)
    reason = request.form.get("reason", "").strip() or None

    session_factory = get_session_factory()
    with session_factory() as session:
        require_period_access(session, period_id)
        try:
            period = lock_period(
                session=session, period_id=period_id, locked_by=changed_by(), reason=reason
            )
        except PeriodActionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.periods_page", company_id=company_id))

    flash(f"Periode {period.period_number} wurde gesperrt.", "success")
    return redirect(url_for("main.periods_page", company_id=company_id))


@main_bp.post("/perioden/<int:period_id>/entsperren")
def unlock_period_action(period_id: int):
    company_id = request.form.get("company_id", type=int)
    user = current_user()
    if user is None or user["role"] != ROLE_ADMIN:
        flash("Perioden entsperren darf nur ein Administrator.", "error")
        return redirect(url_for("main.periods_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_period_access(session, period_id)
        try:
            period = unlock_period(session=session, period_id=period_id, changed_by=changed_by())
        except PeriodActionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.periods_page", company_id=company_id))

    flash(f"Periode {period.period_number} wurde entsperrt.", "success")
    return redirect(url_for("main.periods_page", company_id=company_id))


@main_bp.post("/geschaeftsjahre/<int:fiscal_year_id>/abschliessen")
def close_fiscal_year_action(fiscal_year_id: int):
    company_id = request.form.get("company_id", type=int)
    user = current_user()
    if user is None or user["role"] != ROLE_ADMIN:
        flash("Den Jahresabschluss darf nur ein Administrator durchführen.", "error")
        return redirect(url_for("main.periods_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        fiscal_year = session.get(FiscalYear, fiscal_year_id)
        if fiscal_year is None:
            abort(404)
        require_company_access(session, fiscal_year.company_id)
        try:
            close_result = close_fiscal_year(
                session=session, fiscal_year_id=fiscal_year_id, changed_by=changed_by()
            )
        except PeriodActionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.periods_page", company_id=company_id))

    message = f"Geschäftsjahr {close_result.fiscal_year.label} wurde abgeschlossen."
    if close_result.carryforward_entry is not None:
        message += f" Ergebnisvortrag gebucht ({close_result.carryforward_entry.posting_number})."
    flash(message, "success")
    return redirect(url_for("main.periods_page", company_id=company_id))


@main_bp.post("/geschaeftsjahre")
def create_fiscal_year_action():
    company_id = request.form.get("company_id", type=int)
    user = current_user()
    if user is None or user["role"] != ROLE_ADMIN:
        flash("Geschäftsjahre anlegen darf nur ein Administrator.", "error")
        return redirect(url_for("main.periods_page", company_id=company_id))

    label = request.form.get("label", "").strip()
    start_raw = request.form.get("start_date", "").strip()
    end_raw = request.form.get("end_date", "").strip()
    try:
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    except ValueError:
        flash("Bitte gültige Start- und Enddaten angeben.", "error")
        return redirect(url_for("main.periods_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            fiscal_year = create_fiscal_year(
                session=session,
                company_id=company_id,
                label=label,
                start_date=start_date,
                end_date=end_date,
                changed_by=changed_by(),
            )
        except PeriodActionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.periods_page", company_id=company_id))

    flash(f"Geschäftsjahr {fiscal_year.label} wurde angelegt.", "success")
    return redirect(url_for("main.periods_page", company_id=company_id))


@main_bp.post("/gesellschaften/<int:company_id>/wirtschaftsjahresbeginn")
def set_fiscal_year_start_action(company_id: int):
    user = current_user()
    if user is None or user["role"] != ROLE_ADMIN:
        flash("Den Geschäftsjahresbeginn darf nur ein Administrator ändern.", "error")
        return redirect(url_for("main.periods_page", company_id=company_id))

    start_month = request.form.get("start_month", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            set_fiscal_year_start_month(
                session=session,
                company_id=company_id,
                start_month=start_month or 0,
                changed_by=changed_by(),
            )
        except PeriodActionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.periods_page", company_id=company_id))

    flash("Geschäftsjahresbeginn wurde gespeichert.", "success")
    return redirect(url_for("main.periods_page", company_id=company_id))
