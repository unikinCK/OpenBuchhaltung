"""Ausgangsrechnungen über die REST-API: Entwurf, Ausstellen, Storno, Downloads."""

from __future__ import annotations

import base64
from datetime import date
from pathlib import Path

from flask import current_app, jsonify, request, send_file

from app.api.blueprint import api_bp
from app.api.downloads import download_link_payload, linkable_download
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    bool_arg,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.documents import verify_document_file
from app.services.download_links import DOWNLOAD_LINK_ARG
from app.services.sales_invoices import (
    SalesInvoiceError,
    SalesInvoiceStateError,
    cancel_sales_invoice,
    create_sales_invoice,
    delete_sales_invoice,
    einvoice_hints,
    issue_problems,
    issue_sales_invoice,
    list_sales_invoices,
    serialize_sales_invoice,
    update_sales_invoice,
)
from domain.models import SALES_INVOICE_DRAFT, Document, SalesInvoice

DOWNLOAD_FORMATS = {"pdf": "pdf_document", "xml": "xml_document"}


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _upload_dir() -> Path:
    return Path(current_app.config["DOCUMENT_UPLOAD_DIR"])


def _scoped_invoice(session, invoice_id: int) -> SalesInvoice | None:
    invoice = session.get(SalesInvoice, invoice_id)
    if invoice is None or api_scoped_company(session, invoice.company_id) is None:
        return None
    return invoice


def _not_found():
    return jsonify({"error": "Sales invoice not found."}), 404


def _error_response(exc: SalesInvoiceError):
    status = 409 if isinstance(exc, SalesInvoiceStateError) else 422
    payload: dict[str, object] = {"error": str(exc)}
    if exc.problems:
        payload["problems"] = exc.problems
    return jsonify(payload), status


def _detail(session, invoice: SalesInvoice) -> dict[str, object]:
    """Rechnung mit den Prüfungen, die im Entwurf noch zu erledigen sind."""
    data = serialize_sales_invoice(invoice)
    is_draft = invoice.status == SALES_INVOICE_DRAFT
    data["issue_problems"] = issue_problems(session=session, invoice=invoice) if is_draft else []
    data["einvoice_hints"] = einvoice_hints(session=session, invoice=invoice) if is_draft else []
    return data


def _date_field(payload: dict, field: str) -> date | None:
    value = payload.get(field)
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value))


@api_bp.get("/sales-invoices")
def list_sales_invoices_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    limit = request.args.get("limit", default=100, type=int)
    offset = request.args.get("offset", default=0, type=int)
    if limit is None or offset is None:
        return jsonify({"error": "limit and offset must be integers."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            invoices, total = list_sales_invoices(
                session=session,
                company_id=company_id,
                status=(request.args.get("status") or "").strip() or None,
                partner_id=request.args.get("partner_id", type=int),
                limit=limit,
                offset=offset,
            )
        except SalesInvoiceError as exc:
            return _error_response(exc)
        return jsonify(
            {
                "company_id": company_id,
                "total": total,
                "limit": limit,
                "offset": offset,
                "sales_invoices": [
                    serialize_sales_invoice(invoice, with_lines=False) for invoice in invoices
                ],
            }
        )


@api_bp.get("/sales-invoices/<int:invoice_id>")
def get_sales_invoice_via_api(invoice_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _scoped_invoice(session, invoice_id)
        if invoice is None:
            return _not_found()
        return jsonify(_detail(session, invoice))


@api_bp.post("/sales-invoices")
def create_sales_invoice_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = payload.pop("company_id", None)
    if not isinstance(company_id, int):
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            invoice = create_sales_invoice(
                session=session, company=company, changed_by=_api_changed_by(), **payload
            )
        except SalesInvoiceError as exc:
            session.rollback()
            return _error_response(exc)
        return jsonify(_detail(session, invoice)), 201


@api_bp.patch("/sales-invoices/<int:invoice_id>")
def update_sales_invoice_via_api(invoice_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _scoped_invoice(session, invoice_id)
        if invoice is None:
            return _not_found()
        try:
            invoice = update_sales_invoice(
                session=session, invoice=invoice, changed_by=_api_changed_by(), **payload
            )
        except SalesInvoiceError as exc:
            session.rollback()
            return _error_response(exc)
        return jsonify(_detail(session, invoice))


@api_bp.delete("/sales-invoices/<int:invoice_id>")
# Die MCP-Tools sprechen nur GET, POST und PATCH.
@api_bp.post("/sales-invoices/<int:invoice_id>/delete")
def delete_sales_invoice_via_api(invoice_id: int):
    if not api_can_write():
        return forbidden()
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _scoped_invoice(session, invoice_id)
        if invoice is None:
            return _not_found()
        try:
            delete_sales_invoice(session=session, invoice=invoice, changed_by=_api_changed_by())
        except SalesInvoiceError as exc:
            session.rollback()
            return _error_response(exc)
        return jsonify({"deleted": True, "id": invoice_id})


@api_bp.post("/sales-invoices/<int:invoice_id>/issue")
def issue_sales_invoice_via_api(invoice_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    try:
        issue_date = _date_field(payload, "issue_date")
    except ValueError:
        return jsonify({"error": "issue_date must be an ISO date."}), 400
    receivable_account_id = payload.get("receivable_account_id")
    if receivable_account_id is not None and not isinstance(receivable_account_id, int):
        return jsonify({"error": "receivable_account_id must be an integer."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_invoice(session, invoice_id) is None:
            return _not_found()
        try:
            invoice = issue_sales_invoice(
                session=session,
                invoice_id=invoice_id,
                changed_by=_api_changed_by(),
                upload_dir=_upload_dir(),
                issue_date=issue_date,
                receivable_account_id=receivable_account_id,
            )
        except SalesInvoiceError as exc:
            return _error_response(exc)
        return jsonify(_detail(session, invoice))


@api_bp.post("/sales-invoices/<int:invoice_id>/cancel")
def cancel_sales_invoice_via_api(invoice_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    try:
        cancellation_date = _date_field(payload, "cancellation_date")
    except ValueError:
        return jsonify({"error": "cancellation_date must be an ISO date."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_invoice(session, invoice_id) is None:
            return _not_found()
        try:
            cancellation = cancel_sales_invoice(
                session=session,
                invoice_id=invoice_id,
                changed_by=_api_changed_by(),
                upload_dir=_upload_dir(),
                cancellation_date=cancellation_date,
                reason=payload.get("reason"),
            )
        except SalesInvoiceError as exc:
            return _error_response(exc)
        return jsonify(_detail(session, cancellation))


def _output_document(session, invoice_id: int, file_format: str):
    """Beleg der Rechnung zum Format; sonst ``(None, Fehlerantwort)``."""
    invoice = _scoped_invoice(session, invoice_id)
    if invoice is None:
        return None, _not_found()
    document: Document | None = getattr(invoice, DOWNLOAD_FORMATS[file_format])
    if document is None:
        return None, (jsonify({"error": "Die Rechnung ist noch nicht ausgestellt."}), 409)
    if not Path(document.storage_key).exists():
        return None, (jsonify({"error": "Document file not found."}), 404)
    return document, None


def _send_document(invoice_id: int, file_format: str):
    session_factory = get_session_factory()
    with session_factory() as session:
        document, error = _output_document(session, invoice_id, file_format)
        if error is not None:
            return error
        response = send_file(
            Path(document.storage_key),
            mimetype=document.mime_type,
            as_attachment=True,
            download_name=document.file_name,
        )
        response.headers["X-OpenBuchhaltung-SHA256"] = document.file_sha256
        return response


@api_bp.get("/sales-invoices/<int:invoice_id>/pdf")
@linkable_download
def download_sales_invoice_pdf_via_api(invoice_id: int):
    return _send_document(invoice_id, "pdf")


@api_bp.get("/sales-invoices/<int:invoice_id>/xml")
@linkable_download
def download_sales_invoice_xml_via_api(invoice_id: int):
    return _send_document(invoice_id, "xml")


@api_bp.get("/sales-invoices/<int:invoice_id>/content")
def get_sales_invoice_content_via_api(invoice_id: int):
    """PDF oder XML als Base64 plus Download-Link; ``download_link=1`` nur den Link."""
    file_format = (request.args.get("format") or "pdf").strip().lower()
    if file_format not in DOWNLOAD_FORMATS:
        return jsonify({"error": "format must be pdf or xml."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        document, error = _output_document(session, invoice_id, file_format)
        if error is not None:
            return error
        payload = {
            "sales_invoice_id": invoice_id,
            "document_id": document.id,
            "file_name": document.file_name,
            "mime_type": document.mime_type,
            "file_sha256": document.file_sha256,
            "file_size_bytes": document.file_size_bytes,
            "integrity": verify_document_file(document),
            **download_link_payload(f"/sales-invoices/{invoice_id}/{file_format}"),
        }
        if not bool_arg(DOWNLOAD_LINK_ARG):
            payload["content_base64"] = base64.b64encode(
                Path(document.storage_key).read_bytes()
            ).decode("ascii")
        return jsonify(payload)
