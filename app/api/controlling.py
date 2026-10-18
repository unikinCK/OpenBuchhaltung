"""Kostenstellen, Profitcenter und Controlling-Auswertungen über die REST-API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from flask import jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.controlling import (
    ControllingError,
    controlling_result_report,
    controlling_unit_history,
    create_controlling_unit,
    serialize_controlling_unit,
    update_controlling_unit,
)
from domain.models import ControllingUnit


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _optional_date(payload: dict[str, Any], key: str) -> date | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ControllingError(f"{key} must be an ISO date or null.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ControllingError(f"{key} must be an ISO date (YYYY-MM-DD).") from exc


def _report_json(report: dict[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return convert(report)


@api_bp.get("/controlling-units")
def list_controlling_units():
    company_id = request.args.get("company_id", type=int)
    unit_type = (request.args.get("unit_type") or "").strip() or None
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        stmt = select(ControllingUnit).where(ControllingUnit.company_id == company_id)
        if unit_type:
            if unit_type not in {"cost_center", "profit_center"}:
                return jsonify({"error": "Invalid unit_type."}), 400
            stmt = stmt.where(ControllingUnit.unit_type == unit_type)
        units = session.execute(
            stmt.order_by(ControllingUnit.unit_type, ControllingUnit.code)
        ).scalars().all()
        return jsonify(
            {
                "company_id": company_id,
                "units": [serialize_controlling_unit(unit) for unit in units],
            }
        )


@api_bp.post("/controlling-units")
def create_controlling_unit_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        parent_id = int(payload["parent_id"]) if payload.get("parent_id") is not None else None
        is_active = payload.get("is_active", True)
        session_factory = get_session_factory()
        with session_factory() as session:
            company = api_scoped_company(session, company_id)
            if company is None:
                return jsonify({"error": "Company not found."}), 404
            unit = create_controlling_unit(
                session=session,
                company=company,
                unit_type=payload.get("unit_type", ""),
                code=payload.get("code", ""),
                name=payload.get("name", ""),
                parent_id=parent_id,
                valid_from=_optional_date(payload, "valid_from"),
                valid_to=_optional_date(payload, "valid_to"),
                is_active=is_active,
                changed_by=_api_changed_by(),
            )
            session.commit()
            return jsonify(serialize_controlling_unit(unit)), 201
    except (TypeError, ValueError, ControllingError) as exc:
        return jsonify({"error": str(exc) or "Invalid payload."}), 400
    except IntegrityError:
        return jsonify({"error": "Code already exists for this company and unit type."}), 409


@api_bp.patch("/controlling-units/<int:unit_id>")
def update_controlling_unit_via_api(unit_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    allowed = {"name", "parent_id", "valid_from", "valid_to", "is_active"}
    if not payload:
        return jsonify({"error": "At least one editable field is required."}), 400
    if set(payload) - allowed:
        return jsonify({"error": "code and unit_type are immutable."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        unit = session.get(ControllingUnit, unit_id)
        if unit is None or api_scoped_company(session, unit.company_id) is None:
            return jsonify({"error": "Controlling unit not found."}), 404
        kwargs: dict[str, Any] = {}
        try:
            if "name" in payload:
                kwargs["name"] = payload["name"]
            if "parent_id" in payload:
                kwargs["parent_id"] = (
                    int(payload["parent_id"]) if payload["parent_id"] is not None else None
                )
            if "valid_from" in payload:
                kwargs["valid_from"] = _optional_date(payload, "valid_from")
            if "valid_to" in payload:
                kwargs["valid_to"] = _optional_date(payload, "valid_to")
            if "is_active" in payload:
                kwargs["is_active"] = payload["is_active"]
            changed = update_controlling_unit(
                session=session,
                unit=unit,
                changed_by=_api_changed_by(),
                **kwargs,
            )
            session.commit()
        except (TypeError, ValueError, ControllingError) as exc:
            return jsonify({"error": str(exc) or "Invalid payload."}), 400
        response = serialize_controlling_unit(unit)
        response["changed"] = changed
        return jsonify(response)


@api_bp.get("/controlling-units/<int:unit_id>/history")
def get_controlling_unit_history(unit_id: int):
    limit = request.args.get("limit", default=100, type=int)
    if limit is None:
        return jsonify({"error": "limit must be an integer."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        unit = session.get(ControllingUnit, unit_id)
        if unit is None or api_scoped_company(session, unit.company_id) is None:
            return jsonify({"error": "Controlling unit not found."}), 404
        return jsonify(
            {
                "unit": serialize_controlling_unit(unit),
                "entries": controlling_unit_history(
                    session=session, unit_id=unit.id, limit=limit
                ),
                "limit": max(1, min(limit, 500)),
            }
        )


@api_bp.get("/controlling-report")
def get_controlling_report():
    company_id = request.args.get("company_id", type=int)
    unit_type = (request.args.get("unit_type") or "cost_center").strip()
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
        session_factory = get_session_factory()
        with session_factory() as session:
            if api_scoped_company(session, company_id) is None:
                return jsonify({"error": "Company not found."}), 404
            report = controlling_result_report(
                session=session,
                company_id=company_id,
                unit_type=unit_type,
                date_from=date_from,
                date_to=date_to,
            )
            return jsonify(_report_json(report))
    except (DateArgError, ControllingError) as exc:
        return jsonify({"error": str(exc)}), 400
