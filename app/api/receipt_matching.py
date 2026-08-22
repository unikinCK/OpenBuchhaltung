"""Belegabgleich über die API: Vorschläge erzeugen, listen, freigeben, ablehnen."""

from __future__ import annotations

from datetime import date

from flask import current_app, jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.journal_entries import JournalEntryCreationError, parse_decimal
from app.services.receipt_matching import (
    ReceiptMatchError,
    approve_match_suggestion,
    book_new_booking_suggestion,
    create_match_suggestion,
    reject_suggestion,
)
from app.services.scoping import scoped_select
from domain.models import JournalEntry, ReceiptMatchSuggestion
from domain.services.journal_entry_validation import JournalEntryValidationError


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _suggestion_dict(
    suggestion: ReceiptMatchSuggestion, *, journal_entry: JournalEntry | None = None
) -> dict[str, object]:
    data: dict[str, object] = {
        "id": suggestion.id,
        "company_id": suggestion.company_id,
        "document_id": suggestion.document_id,
        "suggestion_type": suggestion.suggestion_type,
        "journal_entry_id": suggestion.journal_entry_id,
        "confidence": suggestion.confidence,
        "reason": suggestion.reason,
        "llm_used": suggestion.llm_used,
        "status": suggestion.status,
        "supplier": suggestion.supplier,
        "invoice_number": suggestion.invoice_number,
        "invoice_date": (
            suggestion.invoice_date.isoformat() if suggestion.invoice_date else None
        ),
        "net_amount": str(suggestion.net_amount) if suggestion.net_amount is not None else None,
        "tax_amount": str(suggestion.tax_amount) if suggestion.tax_amount is not None else None,
        "gross_amount": (
            str(suggestion.gross_amount) if suggestion.gross_amount is not None else None
        ),
        "tax_rate": str(suggestion.tax_rate) if suggestion.tax_rate is not None else None,
        "currency_code": suggestion.currency_code,
        "created_at": suggestion.created_at.isoformat(),
        "decided_at": suggestion.decided_at.isoformat() if suggestion.decided_at else None,
        "decided_by": suggestion.decided_by,
    }
    if journal_entry is not None:
        data["journal_entry"] = {
            "id": journal_entry.id,
            "posting_number": journal_entry.posting_number,
            "entry_date": journal_entry.entry_date.isoformat(),
            "description": journal_entry.description,
        }
    return data


@api_bp.post("/receipt-matching/suggestions")
def create_receipt_match_suggestion_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        document_id = int(payload.get("document_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and document_id are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        try:
            suggestion = create_match_suggestion(
                session=session,
                company_id=company.id,
                document_id=document_id,
                changed_by=_api_changed_by(),
                ocr_endpoint=current_app.config.get("RECEIPT_OCR_ENDPOINT_URL"),
                ocr_model=current_app.config.get("RECEIPT_OCR_MODEL", "gpt-4.1-mini"),
                llm_endpoint=current_app.config.get("RECEIPT_MATCH_LLM_ENDPOINT_URL"),
                llm_model=current_app.config.get("RECEIPT_MATCH_LLM_MODEL", "gpt-4.1-mini"),
            )
        except ReceiptMatchError as exc:
            return jsonify({"error": str(exc)}), 422

        journal_entry = (
            session.get(JournalEntry, suggestion.journal_entry_id)
            if suggestion.journal_entry_id
            else None
        )
        return jsonify(_suggestion_dict(suggestion, journal_entry=journal_entry)), 201


@api_bp.get("/receipt-matching/suggestions")
def list_receipt_match_suggestions_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    status = (request.args.get("status") or "").strip() or None
    limit = request.args.get("limit", default=50, type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = scoped_select(ReceiptMatchSuggestion, company_id=company.id)
        if status:
            stmt = stmt.where(ReceiptMatchSuggestion.status == status)
        stmt = stmt.order_by(ReceiptMatchSuggestion.id.desc()).limit(max(1, min(limit, 200)))
        suggestions = session.execute(stmt).scalars().all()
        items = []
        for suggestion in suggestions:
            journal_entry = (
                session.get(JournalEntry, suggestion.journal_entry_id)
                if suggestion.journal_entry_id
                else None
            )
            items.append(_suggestion_dict(suggestion, journal_entry=journal_entry))
        return jsonify({"items": items, "count": len(items)})


def _scoped_suggestion(session, company_id: int, suggestion_id: int):
    company = api_scoped_company(session, company_id)
    if company is None:
        return None, (jsonify({"error": "Company not found."}), 404)
    suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
    if suggestion is None or suggestion.company_id != company.id:
        return None, (jsonify({"error": "Suggestion not found."}), 404)
    return suggestion, None


@api_bp.post("/receipt-matching/suggestions/<int:suggestion_id>/approve")
def approve_receipt_match_suggestion_via_api(suggestion_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        suggestion, error = _scoped_suggestion(session, company_id, suggestion_id)
        if error is not None:
            return error

        try:
            if suggestion.suggestion_type == "match":
                journal_entry_id = (
                    int(payload["journal_entry_id"])
                    if payload.get("journal_entry_id") not in (None, "")
                    else None
                )
                suggestion = approve_match_suggestion(
                    session=session,
                    suggestion_id=suggestion.id,
                    changed_by=_api_changed_by(),
                    journal_entry_id=journal_entry_id,
                )
            else:
                expense_account_id = int(payload.get("expense_account_id"))
                creditor_account_id = int(payload.get("creditor_account_id"))
                tax_code_id = (
                    int(payload["tax_code_id"])
                    if payload.get("tax_code_id") not in (None, "")
                    else None
                )
                cost_center_id = (
                    int(payload["cost_center_id"])
                    if payload.get("cost_center_id") not in (None, "")
                    else None
                )
                profit_center_id = (
                    int(payload["profit_center_id"])
                    if payload.get("profit_center_id") not in (None, "")
                    else None
                )
                entry_date = (
                    date.fromisoformat(str(payload["entry_date"]))
                    if payload.get("entry_date")
                    else None
                )
                net_amount = parse_decimal(str(payload.get("net_amount") or "0"))
                tax_amount = parse_decimal(str(payload.get("tax_amount") or "0"))
                suggestion, _entry = book_new_booking_suggestion(
                    session=session,
                    suggestion_id=suggestion.id,
                    changed_by=_api_changed_by(),
                    expense_account_id=expense_account_id,
                    creditor_account_id=creditor_account_id,
                    net_amount=net_amount,
                    tax_amount=tax_amount,
                    tax_code_id=tax_code_id,
                    entry_date=entry_date,
                    description=(payload.get("description") or "").strip() or None,
                    cost_center_id=cost_center_id,
                    profit_center_id=profit_center_id,
                )
        except (TypeError, ValueError) as exc:
            if isinstance(
                exc,
                (ReceiptMatchError, JournalEntryCreationError, JournalEntryValidationError),
            ):
                return jsonify({"error": str(exc)}), 422
            return jsonify({"error": "Invalid payload format."}), 400

        journal_entry = (
            session.get(JournalEntry, suggestion.journal_entry_id)
            if suggestion.journal_entry_id
            else None
        )
        return jsonify(_suggestion_dict(suggestion, journal_entry=journal_entry))


@api_bp.post("/receipt-matching/suggestions/<int:suggestion_id>/reject")
def reject_receipt_match_suggestion_via_api(suggestion_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        suggestion, error = _scoped_suggestion(session, company_id, suggestion_id)
        if error is not None:
            return error

        try:
            suggestion = reject_suggestion(
                session=session,
                suggestion_id=suggestion.id,
                changed_by=_api_changed_by(),
            )
        except ReceiptMatchError as exc:
            return jsonify({"error": str(exc)}), 422

        return jsonify(_suggestion_dict(suggestion))
