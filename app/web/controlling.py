"""UI für Kostenstellen, Profitcenter und deren Ergebnisberichte."""

from __future__ import annotations

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.services.audit_log import list_audit_log_entries
from app.services.controlling import (
    ControllingError,
    controlling_result_report,
    create_controlling_unit,
    update_controlling_unit,
)
from app.web.blueprint import main_bp
from app.web.helpers import changed_by, company_context, get_session_factory, require_company_access
from domain.models import ControllingUnit


def _form_date(name: str) -> date | None:
    raw = request.form.get(name, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ControllingError(f"Ungültiges Datum für {name}.") from exc


@main_bp.get("/controlling")
def controlling_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        units: list[ControllingUnit] = []
        events = []
        cost_center_report = None
        profit_center_report = None
        if selected_company_id:
            units = (
                session.execute(
                    select(ControllingUnit)
                    .where(ControllingUnit.company_id == selected_company_id)
                    .order_by(ControllingUnit.unit_type, ControllingUnit.code)
                )
                .scalars()
                .all()
            )
            events = list_audit_log_entries(
                session=session,
                company_id=selected_company_id,
                entity_type="controlling_unit",
                limit=100,
            )
            cost_center_report = controlling_result_report(
                session=session,
                company_id=selected_company_id,
                unit_type="cost_center",
            )
            profit_center_report = controlling_result_report(
                session=session,
                company_id=selected_company_id,
                unit_type="profit_center",
            )
    return render_template(
        "controlling.html",
        companies=companies,
        selected_company_id=selected_company_id,
        units=units,
        cost_centers=[unit for unit in units if unit.unit_type == "cost_center"],
        profit_centers=[unit for unit in units if unit.unit_type == "profit_center"],
        events=events,
        cost_center_report=cost_center_report,
        profit_center_report=profit_center_report,
    )


@main_bp.post("/controlling-units")
def create_controlling_unit_from_form():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            create_controlling_unit(
                session=session,
                company=company,
                unit_type=request.form.get("unit_type", ""),
                code=request.form.get("code", ""),
                name=request.form.get("name", ""),
                parent_id=request.form.get("parent_id", type=int),
                valid_from=_form_date("valid_from"),
                valid_to=_form_date("valid_to"),
                changed_by=changed_by(),
            )
            session.commit()
        except ControllingError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.controlling_page", company_id=company_id))
        except IntegrityError:
            session.rollback()
            flash("Code ist für Gesellschaft und Typ bereits vergeben.", "error")
            return redirect(url_for("main.controlling_page", company_id=company_id))
    flash("Controlling-Einheit wurde angelegt.", "success")
    return redirect(url_for("main.controlling_page", company_id=company_id))


@main_bp.post("/controlling-units/<int:unit_id>/update")
def update_controlling_unit_from_form(unit_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        unit = session.get(ControllingUnit, unit_id)
        if unit is None:
            abort(404)
        require_company_access(session, unit.company_id)
        try:
            changed = update_controlling_unit(
                session=session,
                unit=unit,
                changed_by=changed_by(),
                name=request.form.get("name", ""),
                parent_id=request.form.get("parent_id", type=int),
                valid_from=_form_date("valid_from"),
                valid_to=_form_date("valid_to"),
                is_active=request.form.get("is_active") == "true",
            )
            session.commit()
            company_id = unit.company_id
        except ControllingError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.controlling_page", company_id=unit.company_id))
    flash(
        "Controlling-Einheit wurde geändert."
        if changed
        else "Keine Änderung erkannt.",
        "success",
    )
    return redirect(url_for("main.controlling_page", company_id=company_id))
