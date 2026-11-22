"""Zahlungsavise über die API: aus einem Beleg auslesen (Hintergrund), Stand abfragen,
neu zuordnen, mit einem Bankumsatz ausgleichen, löschen."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.llm_settings import PURPOSE_RECEIPT, receipt_analysis_options
from app.services.payment_advice import (
    PaymentAdviceError,
    advice_dict,
    apply_advice,
    delete_advice,
    get_advice,
    list_advices,
    rematch_advice,
    start_advice,
)


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _scoped_advice(session, company_id: int | None, advice_id: int):
    if not company_id:
        return None, (jsonify({"error": "company_id is required."}), 400)
    company = api_scoped_company(session, company_id)
    if company is None:
        return None, (jsonify({"error": "Company not found."}), 404)
    try:
        return get_advice(session, company.id, advice_id), None
    except PaymentAdviceError as exc:
        return None, (jsonify({"error": str(exc)}), 404)


@api_bp.get("/payment-advices")
def list_payment_advices_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    limit = request.args.get("limit", default=50, type=int)
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        items = []
        for advice in list_advices(session, company.id, limit=limit):
            data = advice_dict(advice)
            # Die Liste nennt nur die Ampel; Positionen liefert get_payment_advice.
            data.pop("result", None)
            items.append(data)
        return jsonify({"items": items, "count": len(items)})


@api_bp.post("/payment-advices")
def create_payment_advice_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = _int(payload.get("company_id"))
    document_id = _int(payload.get("document_id"))
    if not company_id or not document_id:
        return jsonify({"error": "company_id and document_id are required."}), 400
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            advice = start_advice(
                session=session,
                company_id=company.id,
                document_id=document_id,
                changed_by=_api_changed_by(),
                analysis_options=receipt_analysis_options(
                    llm_purpose=PURPOSE_RECEIPT, session=session
                ),
            )
        except PaymentAdviceError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(advice_dict(advice)), 202


@api_bp.get("/payment-advices/<int:advice_id>")
def get_payment_advice_via_api(advice_id: int):
    company_id = request.args.get("company_id", type=int)
    with_extraction = (request.args.get("include_extraction") or "").lower() in {"1", "true"}
    with get_session_factory()() as session:
        advice, error = _scoped_advice(session, company_id, advice_id)
        if error:
            return error
        return jsonify(advice_dict(advice, with_extraction=with_extraction))


@api_bp.post("/payment-advices/<int:advice_id>/rematch")
def rematch_payment_advice_via_api(advice_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        advice, error = _scoped_advice(session, _int(payload.get("company_id")), advice_id)
        if error:
            return error
        try:
            advice = rematch_advice(session=session, advice=advice, changed_by=_api_changed_by())
        except PaymentAdviceError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(advice_dict(advice))


@api_bp.post("/payment-advices/<int:advice_id>/apply")
def apply_payment_advice_via_api(advice_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    bank_transaction_id = _int(payload.get("bank_transaction_id"))
    if not bank_transaction_id:
        return jsonify({"error": "bank_transaction_id is required."}), 400
    with get_session_factory()() as session:
        advice, error = _scoped_advice(session, _int(payload.get("company_id")), advice_id)
        if error:
            return error
        try:
            advice = apply_advice(
                session=session,
                advice=advice,
                bank_transaction_id=bank_transaction_id,
                changed_by=_api_changed_by(),
                discount_account_id=_int(payload.get("discount_account_id")),
                discount_tax_code_id=_int(payload.get("discount_tax_code_id")),
                deduction_account_id=_int(payload.get("deduction_account_id")),
                deduction_tax_code_id=_int(payload.get("deduction_tax_code_id")),
            )
        except PaymentAdviceError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(advice_dict(advice))


@api_bp.delete("/payment-advices/<int:advice_id>")
@api_bp.post("/payment-advices/<int:advice_id>/delete")
def delete_payment_advice_via_api(advice_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = request.args.get("company_id", type=int) or _int(payload.get("company_id"))
    with get_session_factory()() as session:
        advice, error = _scoped_advice(session, company_id, advice_id)
        if error:
            return error
        try:
            delete_advice(session=session, advice=advice, changed_by=_api_changed_by())
        except PaymentAdviceError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify({"deleted": True, "id": advice_id})
