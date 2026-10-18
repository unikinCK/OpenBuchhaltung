"""SEPA-Zahlläufe über die API."""

from __future__ import annotations

import base64
from datetime import date

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.sepa_export import (
    SepaExportError,
    create_payment_run,
    payable_items_for_run,
    set_company_bank_details,
)


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


@api_bp.post("/companies/<int:company_id>/bank-details")
def set_company_bank_details_via_api(company_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            company = set_company_bank_details(
                session=session,
                company_id=company_id,
                iban=payload.get("iban"),
                bic=payload.get("bic"),
                changed_by=_api_changed_by(),
            )
        except SepaExportError as exc:
            return jsonify({"error": str(exc)}), 422
        return (
            jsonify({"company_id": company.id, "iban": company.iban, "bic": company.bic}),
            200,
        )


@api_bp.get("/payment-runs/proposals")
def list_payment_run_proposals_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        items = payable_items_for_run(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "items": [
                        {
                            "open_item_id": item.id,
                            "reference": item.reference,
                            "counterparty": item.counterparty,
                            "counterparty_iban": item.counterparty_iban,
                            "counterparty_bic": item.counterparty_bic,
                            "due_date": item.due_date.isoformat() if item.due_date else None,
                            "open_amount": str(item.open_amount),
                            "currency_code": item.currency_code,
                        }
                        for item in items
                    ],
                }
            ),
            200,
        )


@api_bp.post("/payment-runs")
def create_payment_run_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        open_item_ids = [int(value) for value in payload.get("open_item_ids") or []]
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and open_item_ids are required."}), 400

    execution_date = None
    if payload.get("execution_date"):
        try:
            execution_date = date.fromisoformat(str(payload["execution_date"]))
        except ValueError:
            return jsonify({"error": "execution_date must be an ISO date."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = create_payment_run(
                session=session,
                company_id=company_id,
                open_item_ids=open_item_ids,
                execution_date=execution_date,
                changed_by=_api_changed_by(),
            )
        except SepaExportError as exc:
            return jsonify({"error": str(exc)}), 422

        return (
            jsonify(
                {
                    "company_id": company_id,
                    "file_name": result.file_name,
                    "transaction_count": result.transaction_count,
                    "control_sum": str(result.control_sum),
                    "open_item_ids": result.open_item_ids,
                    "xml_base64": base64.b64encode(result.xml_bytes).decode("ascii"),
                }
            ),
            201,
        )
