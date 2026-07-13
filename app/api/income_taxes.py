"""Ertragsteuern: Körperschaftsteuer und Gewerbesteuer über die API."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.income_taxes import (
    DECLARATION_TYPE_DECLARATION,
    IncomeTaxError,
    compute_income_tax_return,
    display_declaration_type,
    display_tax_type,
    list_income_tax_returns,
    save_income_tax_return,
)


def _payload() -> dict[str, object]:
    return request.get_json(silent=True) or {}


def _income_tax_return_payload(item) -> dict[str, object]:
    return {
        "id": item.id,
        "company_id": item.company_id,
        "tax_type": item.tax_type,
        "tax_type_label": display_tax_type(item.tax_type),
        "declaration_type": item.declaration_type,
        "declaration_type_label": display_declaration_type(item.declaration_type),
        "period_label": item.period_label,
        "date_from": item.date_from.isoformat(),
        "date_to": item.date_to.isoformat(),
        "status": item.status,
        "calculation": item.calculation,
        "created_at": item.created_at.isoformat(),
        "created_by": item.created_by,
    }


@api_bp.post("/income-tax-returns/preview")
def preview_income_tax_return_via_api():
    payload = _payload()
    try:
        company_id = int(payload.get("company_id"))
        year = payload.get("year")
        tax_type = str(payload.get("tax_type") or "")
    except (TypeError, ValueError):
        return jsonify({"error": "company_id, year and tax_type are required."}), 400
    declaration_type = str(payload.get("declaration_type") or DECLARATION_TYPE_DECLARATION)

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = compute_income_tax_return(
                session=session,
                company_id=company_id,
                year=year,
                tax_type=tax_type,
                declaration_type=declaration_type,
                additions=payload.get("additions"),
                reductions=payload.get("reductions"),
                loss_carryforward=payload.get("loss_carryforward") or "0",
                prepayments=payload.get("prepayments") or "0",
                municipality_multiplier=payload.get("municipality_multiplier"),
                trade_tax_allowance=payload.get("trade_tax_allowance") or "0",
            )
        except IncomeTaxError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result), 200


@api_bp.get("/income-tax-returns")
def list_income_tax_returns_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    tax_type = (request.args.get("tax_type") or "").strip() or None

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            items = list_income_tax_returns(
                session=session, company_id=company_id, tax_type=tax_type
            )
        except IncomeTaxError as exc:
            return jsonify({"error": str(exc)}), 400
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "income_tax_returns": [
                        _income_tax_return_payload(item) for item in items
                    ],
                }
            ),
            200,
        )


@api_bp.post("/income-tax-returns")
def create_income_tax_return_via_api():
    if not api_can_write():
        return forbidden()

    payload = _payload()
    try:
        company_id = int(payload.get("company_id"))
        year = payload.get("year")
        tax_type = str(payload.get("tax_type") or "")
    except (TypeError, ValueError):
        return jsonify({"error": "company_id, year and tax_type are required."}), 400
    declaration_type = str(payload.get("declaration_type") or DECLARATION_TYPE_DECLARATION)

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            item = save_income_tax_return(
                session=session,
                company_id=company_id,
                year=year,
                tax_type=tax_type,
                declaration_type=declaration_type,
                additions=payload.get("additions"),
                reductions=payload.get("reductions"),
                loss_carryforward=payload.get("loss_carryforward") or "0",
                prepayments=payload.get("prepayments") or "0",
                municipality_multiplier=payload.get("municipality_multiplier"),
                trade_tax_allowance=payload.get("trade_tax_allowance") or "0",
                changed_by=(current_api_user() or {}).get("username", "api"),
            )
        except IncomeTaxError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_income_tax_return_payload(item)), 201
