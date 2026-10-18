"""Beleg-OCR über die API."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

from flask import current_app, jsonify, request
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.documents import CAPTURE_FIELDS, json_upload_content, upload_capture
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
    incoming_invoice_error_response,
)
from app.auth import current_api_user
from app.services.audit_log import log_audit_event
from app.services.documents import (
    DOCUMENT_SIGNATURE_PROBE_BYTES,
    document_content_error_code,
    document_file_metadata,
    find_current_document_by_hash,
)
from app.services.incoming_invoice import (
    IncomingInvoiceError,
    book_incoming_invoice,
    invoice_gross_amount,
    parse_tax_splits,
    tax_splits_audit,
)
from app.services.journal_entries import JournalEntryCreationError, parse_decimal
from app.services.llm_settings import PURPOSE_RECEIPT, receipt_analysis_options
from app.services.receipt_ocr import ReceiptExtraction, ReceiptOCRError, analyze_document
from app.services.receipt_proposals import account_choices, propose_receipt_booking
from app.web.helpers import ALLOWED_DOCUMENT_EXTENSIONS, ALLOWED_DOCUMENT_MIME_TYPES
from domain.models import Document
from domain.services.journal_entry_validation import JournalEntryValidationError


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _extraction_dict(extraction: ReceiptExtraction) -> dict[str, object]:
    return {
        "raw_text": extraction.raw_text,
        "supplier": extraction.supplier,
        "invoice_number": extraction.invoice_number,
        "invoice_date": extraction.invoice_date.isoformat() if extraction.invoice_date else None,
        "net_amount": str(extraction.net_amount) if extraction.net_amount is not None else None,
        "tax_amount": str(extraction.tax_amount) if extraction.tax_amount is not None else None,
        "gross_amount": (
            str(extraction.gross_amount) if extraction.gross_amount is not None else None
        ),
        "tax_rate": str(extraction.tax_rate) if extraction.tax_rate is not None else None,
        "currency_code": extraction.currency_code,
        "confidence": extraction.confidence,
        "source": extraction.source,
        "warnings": extraction.warnings,
        "llm_used": extraction.llm_used,
        "control_status": extraction.control_status,
        "has_booking_basis": extraction.has_booking_basis,
        "supplier_vat_id": extraction.supplier_vat_id,
        "description": extraction.description,
        "tip_amount": str(extraction.tip_amount) if extraction.tip_amount else None,
        "reverse_charge": extraction.reverse_charge,
        "tax_lines": [
            {"rate": str(rate), "net_amount": str(net), "tax_amount": str(tax)}
            for rate, net, tax in extraction.tax_lines
        ],
    }


def _load_upload_payload():
    if request.files:
        uploaded_file = request.files.get("document_file")
        if uploaded_file is None or not uploaded_file.filename:
            return None, jsonify({"error": "document_file is required."}), 400
        content = uploaded_file.read()
        return (
            {
                "company_id": request.form.get("company_id", type=int),
                "document_date": request.form.get("document_date"),
                "file_name": secure_filename(uploaded_file.filename),
                "mime_type": uploaded_file.mimetype or "application/octet-stream",
                "content": content,
            },
            None,
            None,
        )

    payload = request.get_json(silent=True) or {}
    upload_content, error_response, status_code = json_upload_content(payload)
    if error_response is not None:
        return None, error_response, status_code
    return (
        {
            "company_id": payload.get("company_id"),
            "document_date": payload.get("document_date"),
            **upload_content,
            **{field: payload.get(field) for field in CAPTURE_FIELDS},
        },
        None,
        None,
    )


_CONTENT_ERROR_MESSAGES = {
    "too_large": "Document is too large.",
    "too_small": "Document is too small and looks incomplete or over-compressed.",
    "signature_mismatch": "Document content does not match the declared file type.",
}


def _validate_upload(*, file_name: str, mime_type: str, content: bytes) -> str | None:
    if not file_name:
        return "file_name is required."
    if Path(file_name).suffix.lower() not in ALLOWED_DOCUMENT_EXTENSIONS:
        return "Only PDF, JPG and PNG documents may be uploaded."
    if mime_type not in ALLOWED_DOCUMENT_MIME_TYPES:
        return "Document MIME type is not allowed."
    error_code = document_content_error_code(
        mime_type=mime_type,
        content_head=content[:DOCUMENT_SIGNATURE_PROBE_BYTES],
        content_size=len(content),
        max_bytes=current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES"),
        min_bytes=current_app.config.get("DOCUMENT_MIN_UPLOAD_BYTES"),
    )
    if error_code:
        return _CONTENT_ERROR_MESSAGES[error_code]
    return None


@api_bp.post("/receipt-ocr/suggestions")
def create_receipt_ocr_suggestion_via_api():
    if not api_can_write():
        return forbidden()

    upload, error_response, status_code = _load_upload_payload()
    if error_response is not None:
        return error_response, status_code
    try:
        company_id = int(upload["company_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400
    try:
        document_date = date.fromisoformat(str(upload["document_date"] or ""))
    except ValueError:
        return jsonify({"error": "document_date is required in YYYY-MM-DD format."}), 400

    file_name = upload["file_name"]
    mime_type = upload["mime_type"]
    content = upload["content"]
    validation_error = _validate_upload(file_name=file_name, mime_type=mime_type, content=content)
    if validation_error:
        return jsonify({"error": validation_error}), 422
    metadata = document_file_metadata(content)
    capture, error_response, status_code = upload_capture(upload, metadata.file_sha256)
    if error_response is not None:
        return error_response, status_code

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        if capture.client_sha256:
            existing = find_current_document_by_hash(
                session, company_id=company.id, file_sha256=metadata.file_sha256
            )
            if existing is not None:
                # Erneut gesendeter Beleg: kein zweiter Beleg, kein zweiter Vorschlag.
                return (
                    jsonify(
                        {
                            "error": "Dieser Beleg ist bereits erfasst.",
                            "duplicate": True,
                            "document_id": existing.id,
                        }
                    ),
                    409,
                )

        unique_name = f"{uuid4().hex}_{file_name}"
        company_dir = (
            Path(current_app.config["DOCUMENT_UPLOAD_DIR"])
            / str(company.tenant_id)
            / str(company.id)
        )
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        target_path.write_bytes(content)

        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name=file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            document_date=document_date,
            capture_source=capture.source,
            captured_at=capture.captured_at,
            capture_device=capture.device,
        )
        session.add(document)
        session.flush()
        document_id = document.id
        choices = account_choices(session, company.id)
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document.id),
            action="uploaded",
            changed_by=_api_changed_by(),
            payload={
                "file_name": document.file_name,
                "mime_type": document.mime_type,
                "file_sha256": document.file_sha256,
                "capture_source": capture.source,
                "capture_device": capture.device,
                "client_hash_verified": capture.client_sha256 is not None,
                "source_host": upload.get("source_host"),
            },
        )
        session.commit()

    try:
        extraction = analyze_document(
            file_bytes=content,
            mime_type=mime_type,
            file_name=file_name,
            account_choices=choices,
            **receipt_analysis_options(llm_purpose=PURPOSE_RECEIPT),
        )
    except ReceiptOCRError as exc:
        return jsonify({"error": str(exc), "document_id": document_id}), 422

    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        document = session.get(Document, document_id)
        if company is None or document is None or document.company_id != company.id:
            return jsonify({"error": "Document not found."}), 404
        if extraction.invoice_date is not None:
            document.document_date = extraction.invoice_date
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document_id),
            action="ocr_analyzed",
            changed_by=_api_changed_by(),
            payload={
                "source": extraction.source,
                "confidence": extraction.confidence,
                "llm_used": extraction.llm_used,
                "control_status": extraction.control_status,
                "gross_amount": str(extraction.gross_amount)
                if extraction.gross_amount is not None
                else None,
                "tax_rate": str(extraction.tax_rate) if extraction.tax_rate is not None else None,
                "supplier": extraction.supplier,
                "invoice_number": extraction.invoice_number,
                "document_date": (
                    document.document_date.isoformat() if document.document_date else None
                ),
            },
        )
        proposal = propose_receipt_booking(session, company.id, extraction).as_dict()
        session.commit()

    return (
        jsonify(
            {
                "document_id": document_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "extraction": _extraction_dict(extraction),
                "booking_proposal": proposal,
            }
        ),
        201,
    )


@api_bp.post("/receipt-ocr/book")
def book_receipt_ocr_suggestion_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        document_id = int(payload.get("document_id"))
        expense_account_id = int(payload.get("expense_account_id"))
        creditor_account_id = int(payload.get("creditor_account_id"))
        tax_code_id = (
            int(payload["tax_code_id"]) if payload.get("tax_code_id") is not None else None
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
        partner_id = (
            int(payload["partner_id"]) if payload.get("partner_id") not in (None, "") else None
        )
        entry_date = date.fromisoformat(payload.get("entry_date") or date.today().isoformat())
        net_amount = parse_decimal(str(payload.get("net_amount") or "0"))
        tax_amount = parse_decimal(str(payload.get("tax_amount") or "0"))
        tax_splits = parse_tax_splits(payload.get("tax_lines"))
        tip_amount = parse_decimal(str(payload.get("tip_amount") or "0"))
    except (JournalEntryCreationError, TypeError, ValueError):
        return jsonify({"error": "Invalid payload format."}), 400

    description = (payload.get("description") or "").strip() or "Belegbuchung (OCR)"
    gross_amount = invoice_gross_amount(net_amount, tax_amount, tax_splits, tip_amount)

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        document = session.get(Document, document_id)
        if document is None or document.company_id != company.id:
            return jsonify({"error": "Document not found."}), 404

        try:
            entry = book_incoming_invoice(
                session=session,
                company=company,
                entry_date=entry_date,
                description=description,
                expense_account_id=expense_account_id,
                creditor_account_id=creditor_account_id,
                net_amount=net_amount,
                tax_amount=tax_amount,
                gross_amount=gross_amount,
                tax_code_id=tax_code_id,
                expense_line_description=description,
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
                partner_id=partner_id,
                tax_splits=tax_splits,
                tip_amount=tip_amount,
                changed_by=_api_changed_by(),
                commit=False,
            )
        except IncomingInvoiceError as exc:
            return incoming_invoice_error_response(exc)
        except (JournalEntryCreationError, JournalEntryValidationError) as exc:
            return jsonify({"error": str(exc)}), 422

        document.journal_entry_id = entry.id
        document.document_date = entry_date
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document.id),
            action="ocr_booked",
            changed_by=_api_changed_by(),
            payload={
                "journal_entry_id": entry.id,
                "net_amount": str(net_amount),
                "tax_amount": str(tax_amount),
                "gross_amount": str(gross_amount),
                "tax_lines": tax_splits_audit(tax_splits),
                "tip_amount": str(tip_amount) if tip_amount > 0 else None,
                "document_date": document.document_date.isoformat(),
                "cost_center_id": cost_center_id,
                "profit_center_id": profit_center_id,
            },
        )
        session.commit()
        return (
            jsonify(
                {
                    "document_id": document.id,
                    "journal_entry_id": entry.id,
                    "posting_number": entry.posting_number,
                    "gross_amount": str(gross_amount),
                }
            ),
            201,
        )
