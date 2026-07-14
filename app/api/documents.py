"""Belegverwaltung über die API."""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.audit_log import log_audit_event
from app.services.documents import document_file_metadata, verify_document_file
from app.services.scoping import scoped_select
from app.web.helpers import ALLOWED_DOCUMENT_EXTENSIONS, ALLOWED_DOCUMENT_MIME_TYPES
from domain.models import Document, JournalEntry


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _document_dict(document: Document) -> dict[str, object]:
    return {
        "id": document.id,
        "tenant_id": document.tenant_id,
        "company_id": document.company_id,
        "journal_entry_id": document.journal_entry_id,
        "file_name": document.file_name,
        "mime_type": document.mime_type,
        "file_sha256": document.file_sha256,
        "file_size_bytes": document.file_size_bytes,
        "version_number": document.version_number,
        "is_current": document.is_current,
        "replaces_document_id": document.replaces_document_id,
        "replaced_at": document.replaced_at.isoformat() if document.replaced_at else None,
        "document_date": document.document_date.isoformat() if document.document_date else None,
        "delete_protected": True,
        "uploaded_at": document.uploaded_at.isoformat(),
    }


def _validate_document_file(*, file_name: str, mime_type: str, content: bytes) -> str | None:
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        return "Only PDF, JPG and PNG documents may be uploaded."
    if mime_type not in ALLOWED_DOCUMENT_MIME_TYPES:
        return "Document MIME type is not allowed."
    max_bytes = current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES")
    if max_bytes and len(content) > max_bytes:
        return "Document is too large."
    return None


def _load_upload_payload():
    if request.files:
        uploaded_file = request.files.get("document_file")
        if uploaded_file is None or not uploaded_file.filename:
            return None, jsonify({"error": "document_file is required."}), 400
        file_name = secure_filename(uploaded_file.filename)
        content = uploaded_file.read()
        return (
            {
                "company_id": request.form.get("company_id", type=int),
                "journal_entry_id": request.form.get("journal_entry_id", type=int),
                "document_date": request.form.get("document_date"),
                "file_name": file_name,
                "mime_type": uploaded_file.mimetype or "application/octet-stream",
                "content": content,
            },
            None,
            None,
        )

    payload = request.get_json(silent=True) or {}
    file_name = secure_filename((payload.get("file_name") or "").strip())
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
            "journal_entry_id": payload.get("journal_entry_id"),
            "document_date": payload.get("document_date"),
            "file_name": file_name,
            "mime_type": payload.get("mime_type") or "application/octet-stream",
            "content": content,
        },
        None,
        None,
    )


@api_bp.get("/documents")
def list_documents_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    journal_entry_id = request.args.get("journal_entry_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        stmt = scoped_select(Document, company_id=company_id).order_by(
            Document.document_date.desc(), Document.uploaded_at.desc()
        )
        if journal_entry_id is not None:
            stmt = stmt.where(Document.journal_entry_id == journal_entry_id)
        documents = session.execute(stmt).scalars().all()
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "documents": [_document_dict(document) for document in documents],
                }
            ),
            200,
        )


@api_bp.post("/documents")
def upload_document_via_api():
    if not api_can_write():
        return forbidden()

    upload, error_response, status_code = _load_upload_payload()
    if error_response is not None:
        return error_response, status_code

    try:
        company_id = int(upload["company_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    file_name = upload["file_name"]
    if not file_name:
        return jsonify({"error": "file_name is required."}), 400
    mime_type = upload["mime_type"]
    content = upload["content"]
    validation_error = _validate_document_file(
        file_name=file_name, mime_type=mime_type, content=content
    )
    if validation_error:
        return jsonify({"error": validation_error}), 422

    raw_journal_entry_id = upload["journal_entry_id"]
    try:
        journal_entry_id = int(raw_journal_entry_id) if raw_journal_entry_id else None
    except (TypeError, ValueError):
        return jsonify({"error": "journal_entry_id must be an integer or null."}), 400

    try:
        document_date = date.fromisoformat(str(upload["document_date"] or ""))
    except ValueError:
        return jsonify({"error": "document_date is required in YYYY-MM-DD format."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        linked_entry = None
        if journal_entry_id is not None:
            linked_entry = session.get(JournalEntry, journal_entry_id)
            if linked_entry is None or linked_entry.company_id != company.id:
                return jsonify({"error": "Journal entry not found."}), 404

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
            journal_entry_id=linked_entry.id if linked_entry else None,
            file_name=file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            document_date=document_date,
        )
        session.add(document)
        session.flush()
        log_audit_event(
            session=session,
            tenant_id=document.tenant_id,
            company_id=document.company_id,
            entity_type="document",
            entity_id=str(document.id),
            action="uploaded",
            changed_by=_api_changed_by(),
            payload={
                "file_name": document.file_name,
                "journal_entry_id": document.journal_entry_id,
                "mime_type": document.mime_type,
                "file_sha256": document.file_sha256,
                "file_size_bytes": document.file_size_bytes,
                "version_number": document.version_number,
                "document_date": document.document_date.isoformat(),
            },
        )
        session.commit()
        return jsonify(_document_dict(document)), 201


@api_bp.post("/documents/<int:document_id>/link")
def link_document_via_api(document_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    journal_entry_id = payload.get("journal_entry_id")
    if journal_entry_id is not None:
        try:
            journal_entry_id = int(journal_entry_id)
        except (TypeError, ValueError):
            return jsonify({"error": "journal_entry_id must be an integer or null."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None or api_scoped_company(session, document.company_id) is None:
            return jsonify({"error": "Document not found."}), 404

        linked_entry = None
        if journal_entry_id is not None:
            linked_entry = session.get(JournalEntry, journal_entry_id)
            if linked_entry is None or linked_entry.company_id != document.company_id:
                return jsonify({"error": "Journal entry not found."}), 404

        previous_journal_entry_id = document.journal_entry_id
        document.journal_entry_id = linked_entry.id if linked_entry else None
        log_audit_event(
            session=session,
            tenant_id=document.tenant_id,
            company_id=document.company_id,
            entity_type="document",
            entity_id=str(document.id),
            action="linked",
            changed_by=_api_changed_by(),
            payload={
                "previous_journal_entry_id": previous_journal_entry_id,
                "journal_entry_id": document.journal_entry_id,
            },
        )
        session.commit()
        return jsonify(_document_dict(document)), 200


@api_bp.post("/documents/<int:document_id>/replace")
def replace_document_via_api(document_id: int):
    if not api_can_write():
        return forbidden()

    upload, error_response, status_code = _load_upload_payload()
    if error_response is not None:
        return error_response, status_code

    file_name = upload["file_name"]
    if not file_name:
        return jsonify({"error": "file_name is required."}), 400
    mime_type = upload["mime_type"]
    content = upload["content"]
    validation_error = _validate_document_file(
        file_name=file_name, mime_type=mime_type, content=content
    )
    if validation_error:
        return jsonify({"error": validation_error}), 422

    session_factory = get_session_factory()
    with session_factory() as session:
        previous = session.get(Document, document_id)
        if previous is None or api_scoped_company(session, previous.company_id) is None:
            return jsonify({"error": "Document not found."}), 404
        if not previous.is_current:
            return jsonify({"error": "Only current document versions can be replaced."}), 409

        unique_name = f"{uuid4().hex}_{file_name}"
        company_dir = (
            Path(current_app.config["DOCUMENT_UPLOAD_DIR"])
            / str(previous.tenant_id)
            / str(previous.company_id)
        )
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        target_path.write_bytes(content)
        metadata = document_file_metadata(content)

        replacement = Document(
            tenant_id=previous.tenant_id,
            company_id=previous.company_id,
            journal_entry_id=previous.journal_entry_id,
            file_name=file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            version_number=previous.version_number + 1,
            replaces_document_id=previous.id,
            document_date=previous.document_date,
        )
        previous.is_current = False
        previous.replaced_at = datetime.now(timezone.utc)
        session.add(replacement)
        session.flush()
        log_audit_event(
            session=session,
            tenant_id=previous.tenant_id,
            company_id=previous.company_id,
            entity_type="document",
            entity_id=str(previous.id),
            action="replaced",
            changed_by=_api_changed_by(),
            payload={
                "replacement_document_id": replacement.id,
                "previous_file_sha256": previous.file_sha256,
                "replacement_file_sha256": replacement.file_sha256,
                "previous_version_number": previous.version_number,
                "replacement_version_number": replacement.version_number,
                "document_date": (
                    replacement.document_date.isoformat()
                    if replacement.document_date
                    else None
                ),
            },
        )
        session.commit()
        return jsonify(_document_dict(replacement)), 201


@api_bp.delete("/documents/<int:document_id>")
def delete_document_via_api(document_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None or api_scoped_company(session, document.company_id) is None:
            return jsonify({"error": "Document not found."}), 404
    return jsonify({"error": "Documents are append-only and cannot be deleted."}), 409


@api_bp.get("/documents/<int:document_id>/download")
def download_document_via_api(document_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None or api_scoped_company(session, document.company_id) is None:
            return jsonify({"error": "Document not found."}), 404
        document_path = Path(document.storage_key)
        if not document_path.exists():
            return jsonify({"error": "Document file not found."}), 404
        return send_file(
            document_path,
            mimetype=document.mime_type,
            as_attachment=True,
            download_name=document.file_name,
        )


@api_bp.get("/documents/<int:document_id>/content")
def get_document_content_via_api(document_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None or api_scoped_company(session, document.company_id) is None:
            return jsonify({"error": "Document not found."}), 404
        document_path = Path(document.storage_key)
        if not document_path.exists():
            return jsonify({"error": "Document file not found."}), 404
        return (
            jsonify(
                {
                    **_document_dict(document),
                    "integrity": verify_document_file(document),
                    "content_base64": base64.b64encode(document_path.read_bytes()).decode("ascii"),
                }
            ),
            200,
        )
