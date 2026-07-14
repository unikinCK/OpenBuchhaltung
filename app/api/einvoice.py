"""E-Rechnung Import/Export über die API."""

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
from app.services.einvoice_export import (
    EInvoiceExportError,
    InvoiceLine,
    OutgoingInvoice,
    Party,
    build_einvoice,
)
from app.services.einvoice_import import EInvoiceParseError, ParsedInvoice, parse_einvoice
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from domain.models import Account, Document, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError

ALLOWED_EINVOICE_MIME_TYPES = {"application/xml", "text/xml", "application/octet-stream"}


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _invoice_dict(invoice: ParsedInvoice) -> dict[str, object]:
    return {
        "invoice_number": invoice.invoice_number,
        "issue_date": invoice.issue_date.isoformat(),
        "seller_name": invoice.seller_name,
        "currency_code": invoice.currency_code,
        "net_total": str(invoice.net_total),
        "tax_total": str(invoice.tax_total),
        "grand_total": str(invoice.grand_total),
        "syntax": invoice.syntax,
        "primary_tax_rate": (
            str(invoice.primary_tax_rate) if invoice.primary_tax_rate is not None else None
        ),
        "tax_lines": [
            {
                "rate": str(line.rate),
                "basis": str(line.basis),
                "tax_amount": str(line.tax_amount),
            }
            for line in invoice.tax_lines
        ],
    }


def _load_import_payload():
    if request.files:
        uploaded_file = request.files.get("einvoice_xml")
        if uploaded_file is None or not uploaded_file.filename:
            return None, jsonify({"error": "einvoice_xml is required."}), 400
        return (
            {
                "company_id": request.form.get("company_id", type=int),
                "expense_account_id": request.form.get("expense_account_id", type=int),
                "creditor_account_id": request.form.get("creditor_account_id", type=int),
                "tax_code_id": request.form.get("tax_code_id", type=int),
                "cost_center_id": request.form.get("cost_center_id", type=int),
                "profit_center_id": request.form.get("profit_center_id", type=int),
                "file_name": secure_filename(uploaded_file.filename),
                "mime_type": uploaded_file.mimetype or "application/xml",
                "content": uploaded_file.read(),
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
            "expense_account_id": payload.get("expense_account_id"),
            "creditor_account_id": payload.get("creditor_account_id"),
            "tax_code_id": payload.get("tax_code_id"),
            "cost_center_id": payload.get("cost_center_id"),
            "profit_center_id": payload.get("profit_center_id"),
            "file_name": secure_filename((payload.get("file_name") or "").strip()),
            "mime_type": payload.get("mime_type") or "application/xml",
            "content": content,
        },
        None,
        None,
    )


def _validate_xml_upload(*, file_name: str, mime_type: str, content: bytes) -> str | None:
    if not file_name:
        return "file_name is required."
    if Path(file_name).suffix.lower() != ".xml":
        return "Only XML e-invoices may be imported."
    if mime_type not in ALLOWED_EINVOICE_MIME_TYPES:
        return "E-invoice MIME type is not allowed."
    max_bytes = current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES")
    if max_bytes and len(content) > max_bytes:
        return "E-invoice is too large."
    return None


def _party_from_payload(prefix: str, payload: dict[str, object]) -> Party:
    def text(name: str, default: str = "") -> str:
        return str(payload.get(f"{prefix}_{name}") or default).strip()

    return Party(
        name=text("name"),
        street=text("street"),
        postal_code=text("postal_code"),
        city=text("city"),
        country_code=text("country_code", "DE") or "DE",
        vat_id=text("vat_id"),
    )


def _parse_export_lines(payload: dict[str, object]) -> list[InvoiceLine]:
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        raise EInvoiceExportError("lines must be a list.")

    lines: list[InvoiceLine] = []
    for index, raw_line in enumerate(raw_lines, start=1):
        if not isinstance(raw_line, dict):
            raise EInvoiceExportError(f"Line {index} must be an object.")
        name = str(raw_line.get("name") or "").strip()
        if not name:
            raise EInvoiceExportError(f"Line {index}: name is required.")
        try:
            lines.append(
                InvoiceLine(
                    name=name,
                    quantity=parse_decimal(str(raw_line.get("quantity") or "0")),
                    unit_price=parse_decimal(str(raw_line.get("unit_price") or "0")),
                    tax_rate=parse_decimal(str(raw_line.get("tax_rate") or "0")),
                )
            )
        except JournalEntryCreationError as exc:
            raise EInvoiceExportError(f"Line {index}: {exc}") from exc
    return lines


@api_bp.post("/einvoices/import")
def import_einvoice_via_api():
    if not api_can_write():
        return forbidden()

    upload, error_response, status_code = _load_import_payload()
    if error_response is not None:
        return error_response, status_code

    try:
        company_id = int(upload["company_id"])
        expense_account_id = int(upload["expense_account_id"])
        creditor_account_id = int(upload["creditor_account_id"])
        tax_code_id = (
            int(upload["tax_code_id"]) if upload.get("tax_code_id") is not None else None
        )
        cost_center_id = (
            int(upload["cost_center_id"])
            if upload.get("cost_center_id") not in (None, "")
            else None
        )
        profit_center_id = (
            int(upload["profit_center_id"])
            if upload.get("profit_center_id") not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and account IDs are required."}), 400

    file_name = upload["file_name"]
    mime_type = upload["mime_type"]
    content = upload["content"]
    validation_error = _validate_xml_upload(
        file_name=file_name, mime_type=mime_type, content=content
    )
    if validation_error:
        return jsonify({"error": validation_error}), 422

    try:
        invoice = parse_einvoice(content)
    except EInvoiceParseError as exc:
        return jsonify({"error": str(exc)}), 422

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        expense_account = session.get(Account, expense_account_id)
        creditor_account = session.get(Account, creditor_account_id)
        for account in (expense_account, creditor_account):
            if account is None or account.company_id != company.id:
                return jsonify({"error": "Account not found."}), 404

        zero = Decimal("0.00")
        lines = [
            JournalLineInput(
                account_id=expense_account.id,
                debit_amount=invoice.net_total,
                credit_amount=zero,
                description=f"{invoice.seller_name} {invoice.invoice_number}".strip(),
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
            )
        ]
        if invoice.tax_total > zero:
            tax_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
            if tax_code is None or tax_code.company_id != company.id:
                return jsonify({"error": "Tax code not found."}), 404
            if tax_code.vat_account_id is None:
                return jsonify({"error": f"Tax code {tax_code.code} has no VAT account."}), 422
            lines.append(
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    debit_amount=invoice.tax_total,
                    credit_amount=zero,
                    description=f"Steuer {invoice.primary_tax_rate}%",
                )
            )
        lines.append(
            JournalLineInput(
                account_id=creditor_account.id,
                debit_amount=zero,
                credit_amount=invoice.grand_total,
            )
        )

        try:
            entry = create_journal_entry(
                session=session,
                payload=JournalEntryInput(
                    company_id=company.id,
                    entry_date=invoice.issue_date,
                    description=(
                        f"E-Rechnung {invoice.invoice_number} ({invoice.seller_name})"
                    ).strip(),
                    status="posted",
                    changed_by=_api_changed_by(),
                    lines=lines,
                ),
            )
        except (JournalEntryCreationError, JournalEntryValidationError) as exc:
            return jsonify({"error": str(exc)}), 422

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
            journal_entry_id=entry.id,
            file_name=file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            document_date=invoice.issue_date,
        )
        session.add(document)
        session.flush()
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="einvoice",
            entity_id=str(entry.id),
            action="imported",
            changed_by=_api_changed_by(),
            payload={
                "invoice_number": invoice.invoice_number,
                "seller": invoice.seller_name,
                "syntax": invoice.syntax,
                "grand_total": str(invoice.grand_total),
                "document_id": document.id,
                "document_date": document.document_date.isoformat(),
                "cost_center_id": cost_center_id,
                "profit_center_id": profit_center_id,
            },
        )
        session.commit()
        return (
            jsonify(
                {
                    "journal_entry_id": entry.id,
                    "posting_number": entry.posting_number,
                    "document_id": document.id,
                    "invoice": _invoice_dict(invoice),
                }
            ),
            201,
        )


@api_bp.post("/einvoices/export")
def export_einvoice_via_api():
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        issue_date = date.fromisoformat(str(payload.get("issue_date") or ""))
        lines = _parse_export_lines(payload)
    except (EInvoiceExportError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "Invalid payload format."}), 400

    syntax = str(payload.get("syntax") or "ubl").strip().lower()
    if syntax not in {"ubl", "cii"}:
        return jsonify({"error": "syntax must be 'ubl' or 'cii'."}), 400

    invoice_number = str(payload.get("invoice_number") or "").strip()
    buyer = _party_from_payload("buyer", payload)
    if not invoice_number or not buyer.name:
        return jsonify({"error": "invoice_number and buyer_name are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        seller = Party(
            name=company.name,
            street=current_app.config.get("SELLER_STREET", ""),
            postal_code=current_app.config.get("SELLER_POSTAL_CODE", ""),
            city=current_app.config.get("SELLER_CITY", ""),
            country_code=current_app.config.get("SELLER_COUNTRY_CODE", "DE"),
            vat_id=current_app.config.get("SELLER_VAT_ID", ""),
        )
        currency_code = company.currency_code

    try:
        invoice = OutgoingInvoice(
            invoice_number=invoice_number,
            issue_date=issue_date,
            seller=seller,
            buyer=buyer,
            lines=lines,
            currency_code=currency_code,
            buyer_reference=str(payload.get("buyer_reference") or "").strip(),
        )
        xml = build_einvoice(invoice, syntax=syntax)
    except EInvoiceExportError as exc:
        return jsonify({"error": str(exc)}), 422

    safe_number = secure_filename(invoice_number) or "rechnung"
    file_name = f"XRechnung_{syntax}_{safe_number}.xml"
    return (
        jsonify(
            {
                "file_name": file_name,
                "mime_type": "application/xml",
                "syntax": syntax,
                "content_base64": base64.b64encode(xml.encode("utf-8")).decode("ascii"),
                "xml": xml,
                "totals": {
                    "net_total": str(invoice.net_total),
                    "tax_total": str(invoice.tax_total),
                    "grand_total": str(invoice.grand_total),
                },
            }
        ),
        201,
    )
