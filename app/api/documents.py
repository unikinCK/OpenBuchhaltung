"""Belegverwaltung über die API."""

from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from flask import current_app, jsonify, request, send_file
from sqlalchemy import func, select
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.audit_log import log_audit_event
from app.services.document_categories import (
    DocumentCategoryError,
    assign_document_category,
    create_document_category,
    document_category_dict,
    list_document_categories,
    resolve_document_category,
    set_document_category_active,
)
from app.services.document_fetch import (
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    DocumentFetchError,
    fetch_document,
    parse_allowed_hosts,
)
from app.services.documents import (
    DOCUMENT_SIGNATURE_PROBE_BYTES,
    DocumentCaptureError,
    document_content_error_code,
    document_file_metadata,
    find_current_document_by_hash,
    parse_document_capture,
    verify_document_file,
)
from app.services.scoping import scoped_select
from app.web.helpers import ALLOWED_DOCUMENT_EXTENSIONS, ALLOWED_DOCUMENT_MIME_TYPES
from domain.models import Document, DocumentCategory, JournalEntry


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _document_dict(document: Document) -> dict[str, object]:
    return {
        "id": document.id,
        "tenant_id": document.tenant_id,
        "company_id": document.company_id,
        "journal_entry_id": document.journal_entry_id,
        "category_id": document.category_id,
        "category_name": document.category.name if document.category else None,
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
        "capture_source": document.capture_source,
        "captured_at": _iso_utc(document.captured_at),
        "capture_device": document.capture_device,
    }


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite liefert naive Zeitstempel (UTC)
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


DOCUMENT_CONTENT_ERROR_MESSAGES = {
    "too_large": "Document is too large.",
    "too_small": "Document is too small and looks incomplete or over-compressed.",
    "signature_mismatch": "Document content does not match the declared file type.",
}


def _validate_document_file(*, file_name: str, mime_type: str, content: bytes) -> str | None:
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
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
        return DOCUMENT_CONTENT_ERROR_MESSAGES[error_code]
    return None


CAPTURE_FIELDS = ("capture_source", "captured_at", "capture_device", "client_sha256")


def upload_capture(upload: dict, file_sha256: str):
    """Erfassungsangaben eines Uploads prüfen; liefert (capture, Fehlerantwort, Status).

    Weicht ``client_sha256`` vom Hash der empfangenen Datei ab, ist bei der Übertragung
    etwas verloren gegangen: nichts speichern, das Gerät sendet erneut (422).
    """
    try:
        capture = parse_document_capture(
            source=upload.get("capture_source"),
            captured_at=upload.get("captured_at"),
            device=upload.get("capture_device"),
            client_sha256=upload.get("client_sha256"),
        )
    except DocumentCaptureError as exc:
        return None, jsonify({"error": str(exc)}), 400
    if capture.client_sha256 and capture.client_sha256 != file_sha256:
        error = "client_sha256 does not match the received file."
        if upload.get("source_url"):
            error = (
                "client_sha256 stimmt nicht mit der über source_url geladenen Datei überein. "
                "Link und Hash prüfen oder die Datei als content_base64 senden."
            )
        return (
            None,
            jsonify(
                {
                    "error": error,
                    "client_sha256": capture.client_sha256,
                    "file_sha256": file_sha256,
                }
            ),
            422,
        )
    return capture, None, None


def json_upload_content(payload: dict):
    """Dateiinhalt eines JSON-Uploads: genau eines von ``content_base64`` oder ``source_url``.

    Liefert (``file_name``, ``mime_type``, ``content``, ``source_url``, ``source_host``)
    oder eine Fehlerantwort. Bei ``source_url`` lädt der Server die Datei selbst
    (app.services.document_fetch); ``file_name``/``mime_type`` dürfen dann fehlen und
    werden aus Content-Disposition/Content-Type der Antwort abgeleitet.
    """
    file_name = secure_filename((payload.get("file_name") or "").strip())
    mime_type = (payload.get("mime_type") or "").strip()
    content_base64 = (payload.get("content_base64") or "").strip()
    source_url = (payload.get("source_url") or "").strip()
    if content_base64 and source_url:
        return (
            None,
            jsonify({"error": "Provide exactly one of content_base64 or source_url."}),
            400,
        )
    if source_url:
        config = current_app.config
        try:
            fetched = fetch_document(
                source_url,
                allowed_hosts=parse_allowed_hosts(config.get("DOCUMENT_FETCH_ALLOWED_HOSTS")),
                max_bytes=int(config.get("DOCUMENT_MAX_UPLOAD_BYTES") or 10485760),
                timeout=float(
                    config.get("DOCUMENT_FETCH_TIMEOUT_SECONDS") or DEFAULT_FETCH_TIMEOUT_SECONDS
                ),
            )
        except DocumentFetchError as exc:
            return (
                None,
                jsonify(
                    {"error": str(exc), "source_url_error": exc.code, "fallback": "content_base64"}
                ),
                exc.status_code,
            )
        return (
            {
                "file_name": file_name or secure_filename(fetched.file_name or ""),
                "mime_type": mime_type or fetched.mime_type or "application/octet-stream",
                "content": fetched.content,
                "source_url": source_url,
                "source_host": urlsplit(source_url).netloc.lower(),
            },
            None,
            None,
        )
    if not content_base64:
        return None, jsonify({"error": "content_base64 or source_url is required."}), 400
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        return None, jsonify({"error": "content_base64 must be valid base64."}), 400
    return (
        {
            "file_name": file_name,
            "mime_type": mime_type or "application/octet-stream",
            "content": content,
            "source_url": None,
            "source_host": None,
        },
        None,
        None,
    )


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
                "category_id": request.form.get("category_id", type=int),
                "document_date": request.form.get("document_date"),
                "file_name": file_name,
                "mime_type": uploaded_file.mimetype or "application/octet-stream",
                "content": content,
                **{field: request.form.get(field) for field in CAPTURE_FIELDS},
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
            "journal_entry_id": payload.get("journal_entry_id"),
            "category_id": payload.get("category_id"),
            "document_date": payload.get("document_date"),
            **upload_content,
            **{field: payload.get(field) for field in CAPTURE_FIELDS},
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
    category_id = request.args.get("category_id", type=int)
    query = (request.args.get("q") or "").strip() or None
    # Belege ohne Buchung: nur aktuelle Fassungen (wie die Dashboard-Kachel).
    unlinked = (request.args.get("unlinked") or "").lower() in {"1", "true", "yes"}
    limit = request.args.get("limit", type=int)
    offset = max(0, request.args.get("offset", type=int) or 0)
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        stmt = scoped_select(Document, company_id=company_id)
        if journal_entry_id is not None:
            stmt = stmt.where(Document.journal_entry_id == journal_entry_id)
        if unlinked:
            stmt = stmt.where(
                Document.journal_entry_id.is_(None), Document.is_current.is_(True)
            )
        if category_id is not None:
            stmt = stmt.where(Document.category_id == category_id)
        if query:
            stmt = stmt.where(Document.file_name.ilike(f"%{query}%"))
        if date_from is not None:
            stmt = stmt.where(Document.document_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(Document.document_date <= date_to)
        total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        stmt = stmt.order_by(
            Document.document_date.desc(), Document.uploaded_at.desc(), Document.id.desc()
        )
        if limit is not None:
            stmt = stmt.limit(max(1, min(limit, 1000))).offset(offset)
        documents = session.execute(stmt).scalars().all()
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "total": total,
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

    raw_category_id = upload["category_id"]
    try:
        category_id = int(raw_category_id) if raw_category_id else None
    except (TypeError, ValueError):
        return jsonify({"error": "category_id must be an integer or null."}), 400

    try:
        document_date = date.fromisoformat(str(upload["document_date"] or ""))
    except ValueError:
        return jsonify({"error": "document_date is required in YYYY-MM-DD format."}), 400

    metadata = document_file_metadata(content)
    capture, error_response, status_code = upload_capture(upload, metadata.file_sha256)
    if error_response is not None:
        return error_response, status_code

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

        try:
            category = resolve_document_category(
                session=session, company_id=company.id, category_id=category_id
            )
        except DocumentCategoryError as exc:
            return jsonify({"error": str(exc)}), 422

        if capture.client_sha256:
            # Idempotenz: Ein Gerät, das nach einem Verbindungsabbruch erneut sendet,
            # bekommt den bereits gespeicherten Beleg statt eines Duplikats.
            existing = find_current_document_by_hash(
                session, company_id=company.id, file_sha256=metadata.file_sha256
            )
            if existing is not None:
                return jsonify({**_document_dict(existing), "duplicate": True}), 200

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
            journal_entry_id=linked_entry.id if linked_entry else None,
            category_id=category.id if category else None,
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
                "category_id": document.category_id,
                "mime_type": document.mime_type,
                "file_sha256": document.file_sha256,
                "file_size_bytes": document.file_size_bytes,
                "version_number": document.version_number,
                "document_date": document.document_date.isoformat(),
                "capture_source": capture.source,
                "captured_at": _iso_utc(capture.captured_at),
                "capture_device": capture.device,
                "client_hash_verified": capture.client_sha256 is not None,
                "source_host": upload.get("source_host"),
            },
        )
        session.commit()
        return jsonify({**_document_dict(document), "duplicate": False}), 201


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


@api_bp.post("/documents/<int:document_id>/category")
def categorize_document_via_api(document_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    category_id = payload.get("category_id")
    if category_id is not None:
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return jsonify({"error": "category_id must be an integer or null."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None or api_scoped_company(session, document.company_id) is None:
            return jsonify({"error": "Document not found."}), 404
        try:
            assign_document_category(
                session=session,
                document=document,
                category_id=category_id,
                changed_by=_api_changed_by(),
            )
        except DocumentCategoryError as exc:
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify(_document_dict(document)), 200


@api_bp.get("/document-categories")
def list_document_categories_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    include_inactive = request.args.get("include_inactive", "1").lower() not in {"0", "false"}

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        categories = list_document_categories(
            session=session, company_id=company_id, include_inactive=include_inactive
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "categories": [document_category_dict(category) for category in categories],
                }
            ),
            200,
        )


@api_bp.post("/document-categories")
def create_document_category_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            category = create_document_category(
                session=session,
                company=company,
                name=str(payload.get("name") or ""),
                changed_by=_api_changed_by(),
            )
        except DocumentCategoryError as exc:
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify(document_category_dict(category)), 201


@api_bp.post("/document-categories/<int:category_id>/active")
def set_document_category_active_via_api(category_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    is_active = payload.get("is_active")
    if not isinstance(is_active, bool):
        return jsonify({"error": "is_active must be a boolean."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        category = session.get(DocumentCategory, category_id)
        if category is None or api_scoped_company(session, category.company_id) is None:
            return jsonify({"error": "Document category not found."}), 404
        set_document_category_active(
            session=session,
            category=category,
            is_active=is_active,
            changed_by=_api_changed_by(),
        )
        session.commit()
        return jsonify(document_category_dict(category)), 200


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
    # Nur der Hash-Abgleich zählt hier; die neue Version gilt immer als API-Erfassung.
    _capture, error_response, status_code = upload_capture(
        upload, document_file_metadata(content).file_sha256
    )
    if error_response is not None:
        return error_response, status_code

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
            category_id=previous.category_id,
            file_name=file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            version_number=previous.version_number + 1,
            replaces_document_id=previous.id,
            document_date=previous.document_date,
            capture_source="api",
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
                "source_host": upload.get("source_host"),
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
