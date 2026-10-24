"""Belegabgleich über die API: Vorschläge erzeugen, listen, freigeben, ablehnen."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.incoming_invoice import parse_tax_splits
from app.services.journal_entries import JournalEntryCreationError, parse_decimal
from app.services.llm_settings import PURPOSE_RECEIPT_MATCH, receipt_analysis_options
from app.services.receipt_matching import (
    ReceiptMatchError,
    approve_match_suggestion,
    book_new_booking_suggestion,
    create_match_suggestion,
    entry_gross_total,
    reject_suggestion,
)
from app.services.scoping import scoped_select
from app.services.standard_accounts import VERBINDLICHKEITEN_LUL, find_standard_account
from domain.models import Account, BusinessPartner, ReceiptMatchSuggestion, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _default_creditor(session, company_id: int) -> Account | None:
    """Gegenkonto, das die Freigabe einer neuen Buchung vorschlägt (wie die Maske)."""
    return find_standard_account(
        session=session, company_id=company_id, standard=VERBINDLICHKEITEN_LUL
    )


def _suggestion_dict(
    session, suggestion: ReceiptMatchSuggestion, *, creditor: Account | None = None
) -> dict[str, object]:
    """Vorschlag mit Beleg, vorgeschlagener Buchung und lesbarer Kontierung.

    ``creditor`` ist das vorgeschlagene Gegenkonto einer neuen Buchung
    (Verbindlichkeiten aLuL); Clients übergeben es bei der Freigabe als
    ``creditor_account_id``.
    """
    expense_account = (
        session.get(Account, suggestion.expense_account_id)
        if suggestion.expense_account_id
        else None
    )
    tax_code = session.get(TaxCode, suggestion.tax_code_id) if suggestion.tax_code_id else None
    partner = (
        session.get(BusinessPartner, suggestion.partner_id) if suggestion.partner_id else None
    )
    document = suggestion.document
    journal_entry = suggestion.journal_entry
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
        "supplier_vat_id": suggestion.supplier_vat_id,
        "description": suggestion.description,
        "tip_amount": str(suggestion.tip_amount) if suggestion.tip_amount is not None else None,
        "reverse_charge": suggestion.reverse_charge,
        "booking_proposal": {
            "source": suggestion.proposal_source,
            "reason": suggestion.proposal_reason,
            "expense_account_id": suggestion.expense_account_id,
            "expense_account_code": expense_account.code if expense_account else None,
            "expense_account_name": expense_account.name if expense_account else None,
            "tax_code_id": suggestion.tax_code_id,
            "tax_code": tax_code.code if tax_code else None,
            "partner_id": suggestion.partner_id,
            "partner_name": partner.name if partner else None,
            "creditor_account_id": creditor.id if creditor else None,
            "creditor_account_code": creditor.code if creditor else None,
            "creditor_account_name": creditor.name if creditor else None,
            # Aufteilung je Steuersatz (nur bei mehreren Sätzen), direkt als
            # tax_lines an die Freigabe übergebbar.
            "tax_lines": suggestion.tax_lines or [],
        },
        "document": (
            {
                "id": document.id,
                "file_name": document.file_name,
                "mime_type": document.mime_type,
                "document_date": (
                    document.document_date.isoformat() if document.document_date else None
                ),
                "journal_entry_id": document.journal_entry_id,
            }
            if document is not None
            else None
        ),
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
            "amount": str(entry_gross_total(journal_entry)),
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
                **receipt_analysis_options(
                    llm_purpose=PURPOSE_RECEIPT_MATCH, session=session
                ),
            )
        except ReceiptMatchError as exc:
            return jsonify({"error": str(exc)}), 422

        creditor = _default_creditor(session, company.id)
        return jsonify(_suggestion_dict(session, suggestion, creditor=creditor)), 201


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
        creditor = _default_creditor(session, company.id)
        items = [
            _suggestion_dict(session, suggestion, creditor=creditor)
            for suggestion in suggestions
        ]
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
                expense_account_id = (
                    int(payload["expense_account_id"])
                    if payload.get("expense_account_id") not in (None, "")
                    else None
                )
                partner_id = (
                    int(payload["partner_id"])
                    if payload.get("partner_id") not in (None, "")
                    else None
                )
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
                tax_splits = parse_tax_splits(payload.get("tax_lines"))
                tip_amount = parse_decimal(str(payload.get("tip_amount") or "0"))
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
                    partner_id=partner_id,
                    tax_splits=tax_splits,
                    tip_amount=tip_amount,
                )
        except (TypeError, ValueError) as exc:
            if isinstance(
                exc,
                (ReceiptMatchError, JournalEntryCreationError, JournalEntryValidationError),
            ):
                return jsonify({"error": str(exc)}), 422
            return jsonify({"error": "Invalid payload format."}), 400

        creditor = _default_creditor(session, suggestion.company_id)
        return jsonify(_suggestion_dict(session, suggestion, creditor=creditor))


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

        creditor = _default_creditor(session, suggestion.company_id)
        return jsonify(_suggestion_dict(session, suggestion, creditor=creditor))
