"""Beleg-OCR über die API."""

from __future__ import annotations

import base64
import binascii
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from flask import current_app, jsonify, request
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.audit_log import log_audit_event
from app.services.documents import document_file_metadata
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.receipt_ocr import ReceiptExtraction, ReceiptOCRError, analyze_document
from app.web.helpers import ALLOWED_DOCUMENT_EXTENSIONS, ALLOWED_DOCUMENT_MIME_TYPES
from domain.models import Account, Document, TaxCode
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
    content_base64 = (payload.get("content_base64") or "").strip()
    if not content_base64:
        return None, jsonify({"error": "content_base64 is required."}), 400
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        return None, jsonify({"error": "content_base64 must be valid base64."}), 400
    return (
        {
            "company_id": payload.get("company_id"),
            "document_date": payload.get("document_date"),
            "file_name": secure_filename((payload.get("file_name") or "").strip()),
            "mime_type": payload.get("mime_type") or "application/octet-stream",
            "content": content,
        },
        None,
        None,
    )


def _validate_upload(*, file_name: str, mime_type: str, content: bytes) -> str | None:
    if not file_name:
        return "file_name is required."
    if Path(file_name).suffix.lower() not in ALLOWED_DOCUMENT_EXTENSIONS:
        return "Only PDF, JPG and PNG documents may be uploaded."
    if mime_type not in ALLOWED_DOCUMENT_MIME_TYPES:
        return "Document MIME type is not allowed."
    max_bytes = current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES")
    if max_bytes and len(content) > max_bytes:
        return "Document is too large."
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

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        unique_name = f"{uuid4().hex}_{file_name}"
        company_dir = (
            Path(current_app.config["DOCUMENT_UPLOAD_DIR"])
            / str(company.tenant_id)
            / str(company.id)
        )
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        target_path.write_bytes(content)
        metadata = document_file_metadata(content)

        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name=file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            document_date=document_date,
        )
        session.add(document)
        session.flush()
        document_id = document.id
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document.id),
            action="uploaded",
            changed_by=_api_changed_by(),
            payload={"file_name": document.file_name, "mime_type": document.mime_type},
        )
        session.commit()

    try:
        extraction = analyze_document(
            file_bytes=content,
            mime_type=mime_type,
            file_name=file_name,
            ocr_endpoint=current_app.config.get("RECEIPT_OCR_ENDPOINT_URL"),
            ocr_model=current_app.config.get("RECEIPT_OCR_MODEL", "gpt-4.1-mini"),
            llm_endpoint=current_app.config.get("RECEIPT_LLM_ENDPOINT_URL"),
            llm_model=current_app.config.get("RECEIPT_LLM_MODEL", "gpt-4.1-mini"),
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
        session.commit()

    return (
        jsonify(
            {
                "document_id": document_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "extraction": _extraction_dict(extraction),
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
        entry_date = date.fromisoformat(payload.get("entry_date") or date.today().isoformat())
        net_amount = parse_decimal(str(payload.get("net_amount") or "0"))
        tax_amount = parse_decimal(str(payload.get("tax_amount") or "0"))
    except (JournalEntryCreationError, TypeError, ValueError):
        return jsonify({"error": "Invalid payload format."}), 400

    description = (payload.get("description") or "").strip() or "Belegbuchung (OCR)"
    zero = Decimal("0.00")
    gross_amount = (net_amount + tax_amount).quantize(zero)

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        document = session.get(Document, document_id)
        if document is None or document.company_id != company.id:
            return jsonify({"error": "Document not found."}), 404

        expense_account = session.get(Account, expense_account_id)
        creditor_account = session.get(Account, creditor_account_id)
        for account in (expense_account, creditor_account):
            if account is None or account.company_id != company.id:
                return jsonify({"error": "Account not found."}), 404

        lines = [
            JournalLineInput(
                account_id=expense_account.id,
                debit_amount=net_amount,
                credit_amount=zero,
                description=description,
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
            )
        ]
        if tax_amount > zero:
            tax_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
            if tax_code is None or tax_code.company_id != company.id:
                return jsonify({"error": "Tax code not found."}), 404
            if tax_code.vat_account_id is None:
                return jsonify({"error": f"Tax code {tax_code.code} has no VAT account."}), 422
            lines.append(
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    debit_amount=tax_amount,
                    credit_amount=zero,
                    description=f"Vorsteuer ({tax_code.code})",
                )
            )
        lines.append(
            JournalLineInput(
                account_id=creditor_account.id,
                debit_amount=zero,
                credit_amount=gross_amount,
            )
        )

        try:
            entry = create_journal_entry(
                session=session,
                payload=JournalEntryInput(
                    company_id=company.id,
                    entry_date=entry_date,
                    description=description,
                    status="posted",
                    changed_by=_api_changed_by(),
                    lines=lines,
                ),
            )
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
