"""Perioden und Geschäftsjahre über die API."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request
from sqlalchemy import select

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import ROLE_ADMIN, current_api_user
from app.services.periods import (
    PeriodActionError,
    close_fiscal_year,
    create_fiscal_year,
    lock_period,
    set_fiscal_year_start_month,
    unlock_period,
)
from domain.models import FiscalYear, Period


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _api_can_admin() -> bool:
    user = current_api_user()
    return user is None or user["role"] == ROLE_ADMIN


def _fiscal_year_dict(fiscal_year: FiscalYear, periods: list[Period]) -> dict[str, object]:
    return {
        "id": fiscal_year.id,
        "tenant_id": fiscal_year.tenant_id,
        "company_id": fiscal_year.company_id,
        "label": fiscal_year.label,
        "start_date": fiscal_year.start_date.isoformat(),
        "end_date": fiscal_year.end_date.isoformat(),
        "is_closed": fiscal_year.is_closed,
        "periods": [_period_dict(period) for period in periods],
    }


def _period_dict(period: Period) -> dict[str, object]:
    return {
        "id": period.id,
        "tenant_id": period.tenant_id,
        "fiscal_year_id": period.fiscal_year_id,
        "period_number": period.period_number,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "status": period.status,
        "is_closing": period.is_closing,
    }


@api_bp.get("/fiscal-years")
def list_fiscal_years_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        fiscal_years = (
            session.execute(
                select(FiscalYear)
                .where(FiscalYear.company_id == company_id)
                .order_by(FiscalYear.label.desc())
            )
            .scalars()
            .all()
        )
        periods = (
            session.execute(
                select(Period)
                .where(Period.fiscal_year_id.in_([year.id for year in fiscal_years]))
                .order_by(Period.period_number)
            )
            .scalars()
            .all()
            if fiscal_years
            else []
        )
        periods_by_year: dict[int, list[Period]] = {}
        for period in periods:
            periods_by_year.setdefault(period.fiscal_year_id, []).append(period)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "fiscal_year_start_month": company.fiscal_year_start_month,
                    "fiscal_years": [
                        _fiscal_year_dict(year, periods_by_year.get(year.id, []))
                        for year in fiscal_years
                    ],
                }
            ),
            200,
        )


@api_bp.post("/fiscal-years")
def create_fiscal_year_via_api():
    if not api_can_write():
        return forbidden()
    if not _api_can_admin():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        start_date = date.fromisoformat(payload.get("start_date"))
        end_date = date.fromisoformat(payload.get("end_date"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id, start_date and end_date are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            fiscal_year = create_fiscal_year(
                session=session,
                company_id=company_id,
                label=(payload.get("label") or "").strip(),
                start_date=start_date,
                end_date=end_date,
                changed_by=_api_changed_by(),
            )
        except PeriodActionError as exc:
            return jsonify({"error": str(exc)}), 422
        periods = (
            session.execute(
                select(Period)
                .where(Period.fiscal_year_id == fiscal_year.id)
                .order_by(Period.period_number)
            )
            .scalars()
            .all()
        )
        return jsonify(_fiscal_year_dict(fiscal_year, periods)), 201


@api_bp.post("/periods/<int:period_id>/lock")
def lock_period_via_api(period_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        period = session.get(Period, period_id)
        if period is None:
            return jsonify({"error": "Period not found."}), 404
        fiscal_year = session.get(FiscalYear, period.fiscal_year_id)
        if api_scoped_company(session, fiscal_year.company_id) is None:
            return jsonify({"error": "Period not found."}), 404
        try:
            period = lock_period(
                session=session,
                period_id=period_id,
                locked_by=_api_changed_by(),
                reason=(payload.get("reason") or "").strip() or None,
            )
        except PeriodActionError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_period_dict(period)), 200


@api_bp.post("/periods/<int:period_id>/unlock")
def unlock_period_via_api(period_id: int):
    if not api_can_write():
        return forbidden()
    if not _api_can_admin():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        period = session.get(Period, period_id)
        if period is None:
            return jsonify({"error": "Period not found."}), 404
        fiscal_year = session.get(FiscalYear, period.fiscal_year_id)
        if api_scoped_company(session, fiscal_year.company_id) is None:
            return jsonify({"error": "Period not found."}), 404
        try:
            period = unlock_period(
                session=session, period_id=period_id, changed_by=_api_changed_by()
            )
        except PeriodActionError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_period_dict(period)), 200


@api_bp.post("/fiscal-years/<int:fiscal_year_id>/close")
def close_fiscal_year_via_api(fiscal_year_id: int):
    if not api_can_write():
        return forbidden()
    if not _api_can_admin():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        fiscal_year = session.get(FiscalYear, fiscal_year_id)
        if fiscal_year is None or api_scoped_company(session, fiscal_year.company_id) is None:
            return jsonify({"error": "Fiscal year not found."}), 404
        try:
            result = close_fiscal_year(
                session=session, fiscal_year_id=fiscal_year_id, changed_by=_api_changed_by()
            )
        except PeriodActionError as exc:
            return jsonify({"error": str(exc)}), 422
        periods = (
            session.execute(
                select(Period)
                .where(Period.fiscal_year_id == result.fiscal_year.id)
                .order_by(Period.period_number)
            )
            .scalars()
            .all()
        )
        return (
            jsonify(
                {
                    **_fiscal_year_dict(result.fiscal_year, periods),
                    "carryforward_entry_id": (
                        result.carryforward_entry.id if result.carryforward_entry else None
                    ),
                }
            ),
            200,
        )


@api_bp.post("/companies/<int:company_id>/fiscal-year-start")
def set_fiscal_year_start_via_api(company_id: int):
    if not api_can_write():
        return forbidden()
    if not _api_can_admin():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        start_month = int(payload.get("start_month"))
    except (TypeError, ValueError):
        return jsonify({"error": "start_month is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            company = set_fiscal_year_start_month(
                session=session,
                company_id=company_id,
                start_month=start_month,
                changed_by=_api_changed_by(),
            )
        except PeriodActionError as exc:
            return jsonify({"error": str(exc)}), 422
        return (
            jsonify(
                {
                    "id": company.id,
                    "tenant_id": company.tenant_id,
                    "fiscal_year_start_month": company.fiscal_year_start_month,
                }
            ),
            200,
        )
