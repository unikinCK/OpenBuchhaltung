"""Firmenwissen je Gesellschaft (ADR-004) über die API: Einträge, Quellen, Upload, Paketimport.

Über den MCP-Server (externe KI-Clients) ist das Firmenwissen nur lesbar und änderbar,
wenn die Gesellschaft es dafür freigegeben hat (``knowledge_mcp_enabled``); Obi im
KI-Chat, die Oberfläche und die übrige REST-API betrifft der Schalter nicht. Den Schalter
selbst gibt es bewusst nicht über MCP – ein MCP-Client könnte sich sonst selbst freischalten.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Any

from flask import jsonify, request, send_file
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.documents import json_upload_content
from app.api.downloads import download_link_payload, linkable_download
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    bool_arg,
    bool_payload,
    forbidden,
    get_session_factory,
    mcp_channel,
)
from app.auth import ROLE_ADMIN, current_api_user
from app.services.company_knowledge import (
    SOURCE_FIELDS,
    CompanyKnowledgeError,
    create_entry,
    entry_dict,
    get_entry,
    get_source,
    import_package,
    list_entries,
    list_sources,
    parse_date_arg,
    set_entry_status,
    set_knowledge_mcp,
    set_source_active,
    source_dict,
    update_entry,
    update_source,
)
from app.services.company_knowledge_upload import (
    UPLOAD_DONE,
    get_upload,
    list_uploads,
    retry_upload,
    start_upload,
    upload_dict,
)
from app.services.llm_settings import PURPOSE_KNOWLEDGE, receipt_analysis_options

_ENTRY_FIELDS = {
    "title",
    "content",
    "kind",
    "topic",
    "source_id",
    "locator",
    "review_due",
    "review_note",
}


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _optional_int(value: Any, name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise CompanyKnowledgeError(f"{name}: Ganzzahl erwartet.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CompanyKnowledgeError(f"{name}: Ganzzahl erwartet.") from exc


MCP_DISABLED_ERROR = (
    "Das Firmenwissen dieser Gesellschaft ist für MCP-Clients nicht freigegeben."
)


def _blocked_for_mcp(company) -> bool:
    return company is not None and mcp_channel() and not company.knowledge_mcp_enabled


def _knowledge_company(session, company_id: int | None, *, not_found: str = "Company not found."):
    """Gesellschaft im Zugriffsbereich samt MCP-Freigabe: ``(company, fehlerantwort)``."""
    company = api_scoped_company(session, company_id) if company_id is not None else None
    if company is None:
        return None, (jsonify({"error": not_found}), 404)
    if _blocked_for_mcp(company):
        return None, (jsonify({"error": MCP_DISABLED_ERROR}), 403)
    return company, None


def _scoped_entry(session, entry_id: int):
    entry = get_entry(session=session, entry_id=entry_id)
    _company, error = _knowledge_company(
        session, entry.company_id if entry else None, not_found="Knowledge entry not found."
    )
    return (None, error) if error is not None else (entry, None)


def _scoped_source(session, source_id: int):
    source = get_source(session=session, source_id=source_id)
    _company, error = _knowledge_company(
        session, source.company_id if source else None, not_found="Knowledge source not found."
    )
    return (None, error) if error is not None else (source, None)


@api_bp.get("/companies/<int:company_id>/knowledge/entries")
def list_company_knowledge_entries_via_api(company_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        _company, error = _knowledge_company(session, company_id)
        if error is not None:
            return error
        try:
            entries, total = list_entries(
                session=session,
                company_id=company_id,
                status=request.args.get("status"),
                kind=request.args.get("kind"),
                due_only=bool_arg("due"),
                query=request.args.get("q"),
                source_id=_optional_int(request.args.get("source_id"), "source_id"),
                limit=request.args.get("limit", type=int) or 100,
                offset=request.args.get("offset", type=int) or 0,
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "company_id": company_id,
                "total": total,
                "entries": [entry_dict(entry, with_content=False) for entry in entries],
            }
        )


@api_bp.post("/companies/<int:company_id>/knowledge/entries")
def create_company_knowledge_entry_via_api(company_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        company, error = _knowledge_company(session, company_id)
        if error is not None:
            return error
        try:
            entry = create_entry(
                session=session,
                company=company,
                title=payload.get("title"),
                content=payload.get("content"),
                kind=payload.get("kind"),
                topic=payload.get("topic"),
                source_id=_optional_int(payload.get("source_id"), "source_id"),
                locator=payload.get("locator"),
                review_due=parse_date_arg(payload.get("review_due"), "review_due"),
                review_note=payload.get("review_note"),
                entry_key=payload.get("entry_key"),
                changed_by=_api_changed_by(),
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify(entry_dict(entry)), 201


@api_bp.get("/knowledge-entries/<int:entry_id>")
def get_company_knowledge_entry_via_api(entry_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        entry, error = _scoped_entry(session, entry_id)
        if error is not None:
            return error
        return jsonify(entry_dict(entry))


@api_bp.patch("/knowledge-entries/<int:entry_id>")
def update_company_knowledge_entry_via_api(entry_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"error": "At least one editable field is required."}), 400
    unknown = set(payload) - _ENTRY_FIELDS
    if unknown:
        return jsonify({"error": f"Not editable: {', '.join(sorted(unknown))}."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        entry, error = _scoped_entry(session, entry_id)
        if error is not None:
            return error
        kwargs: dict[str, Any] = {
            key: payload[key] for key in ("title", "content", "kind", "topic", "locator")
            if key in payload
        }
        try:
            if "source_id" in payload:
                kwargs["source_id"] = _optional_int(payload["source_id"], "source_id")
            if "review_due" in payload:
                kwargs["review_due"] = parse_date_arg(payload["review_due"], "review_due")
            if "review_note" in payload:
                kwargs["review_note"] = payload["review_note"]
            changed = update_entry(
                session=session, entry=entry, changed_by=_api_changed_by(), **kwargs
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify({**entry_dict(entry), "changed": changed})


@api_bp.post("/knowledge-entries/<int:entry_id>/status")
def set_company_knowledge_entry_status_via_api(entry_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        entry, error = _scoped_entry(session, entry_id)
        if error is not None:
            return error
        try:
            set_entry_status(
                session=session,
                entry=entry,
                status=str(payload.get("status") or ""),
                changed_by=_api_changed_by(),
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify(entry_dict(entry))


@api_bp.get("/companies/<int:company_id>/knowledge/sources")
def list_company_knowledge_sources_via_api(company_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        _company, error = _knowledge_company(session, company_id)
        if error is not None:
            return error
        sources = list_sources(
            session=session,
            company_id=company_id,
            include_inactive=bool_arg("include_inactive", default=True),
            query=request.args.get("q"),
        )
        return jsonify({"company_id": company_id, "sources": sources})


@api_bp.get("/knowledge-sources/<int:source_id>")
def get_company_knowledge_source_via_api(source_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        source, error = _scoped_source(session, source_id)
        if error is not None:
            return error
        return jsonify(source_dict(source, with_excerpts=True))


@api_bp.post("/knowledge-sources/<int:source_id>/active")
def set_company_knowledge_source_active_via_api(source_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    is_active = payload.get("is_active")
    if not isinstance(is_active, bool):
        return jsonify({"error": "is_active must be a boolean."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        source, error = _scoped_source(session, source_id)
        if error is not None:
            return error
        set_source_active(
            session=session, source=source, is_active=is_active, changed_by=_api_changed_by()
        )
        session.commit()
        return jsonify(source_dict(source))


@api_bp.patch("/knowledge-sources/<int:source_id>")
def update_company_knowledge_source_via_api(source_id: int):
    """Angaben einer hochgeladenen Quelle ändern (Quellen aus Paketen pflegt das Paket)."""
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"error": "At least one editable field is required."}), 400
    with get_session_factory()() as session:
        source, error = _scoped_source(session, source_id)
        if error is not None:
            return error
        try:
            changed = update_source(
                session=session, source=source, changed_by=_api_changed_by(), fields=payload
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify({**source_dict(source), "changed": changed})


def _scoped_upload(session, upload_id: int):
    upload = get_upload(session, upload_id)
    _company, error = _knowledge_company(
        session, upload.company_id if upload else None, not_found="Knowledge upload not found."
    )
    return (None, error) if error is not None else (upload, None)


def _upload_response(upload) -> dict[str, Any]:
    """Stand eines Uploads; fertig mit Entwurf zum Prüfen."""
    data = upload_dict(upload)
    if upload.status == UPLOAD_DONE and upload.entry is not None:
        data["entry"] = entry_dict(upload.entry)
    return data


@api_bp.post("/companies/<int:company_id>/knowledge/uploads")
def upload_company_knowledge_via_api(company_id: int):
    """Unterlage hochladen (202), Verarbeitung im Hintergrund; Stand über GET …/<id>."""
    if not api_can_write():
        return forbidden()
    if request.files:
        file = request.files.get("document_file")
        if file is None or not file.filename:
            return jsonify({"error": "document_file is required."}), 400
        file_name, content = secure_filename(file.filename), file.read()
        fields = {name: request.form[name] for name in SOURCE_FIELDS if name in request.form}
    else:
        payload = request.get_json(silent=True) or {}
        upload_content, error_response, status_code = json_upload_content(payload)
        if error_response is not None:
            return error_response, status_code
        file_name, content = upload_content["file_name"], upload_content["content"]
        if not file_name:
            return jsonify({"error": "file_name is required."}), 400
        fields = {name: payload[name] for name in SOURCE_FIELDS if name in payload}
    with get_session_factory()() as session:
        company, error = _knowledge_company(session, company_id)
        if error is not None:
            return error
        try:
            upload, duplicate = start_upload(
                session=session,
                company=company,
                file_name=file_name,
                content=content,
                changed_by=_api_changed_by(),
                analysis_options=receipt_analysis_options(
                    llm_purpose=PURPOSE_KNOWLEDGE, session=session
                ),
                fields=fields,
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 422
        if duplicate:
            return jsonify({**_upload_response(upload), "duplicate": True}), 200
        return jsonify(_upload_response(upload)), 202


@api_bp.get("/companies/<int:company_id>/knowledge/uploads")
def list_company_knowledge_uploads_via_api(company_id: int):
    with get_session_factory()() as session:
        _company, error = _knowledge_company(session, company_id)
        if error is not None:
            return error
        uploads = list_uploads(
            session, company_id, limit=request.args.get("limit", type=int) or 50
        )
        return jsonify({"company_id": company_id, "uploads": [upload_dict(u) for u in uploads]})


@api_bp.get("/knowledge-uploads/<int:upload_id>")
def get_company_knowledge_upload_via_api(upload_id: int):
    with get_session_factory()() as session:
        upload, error = _scoped_upload(session, upload_id)
        if error is not None:
            return error
        return jsonify(
            {
                **_upload_response(upload),
                **download_link_payload(f"/knowledge-uploads/{upload.id}/file"),
            }
        )


@api_bp.get("/knowledge-uploads/<int:upload_id>/file")
@linkable_download
def download_company_knowledge_upload_via_api(upload_id: int):
    with get_session_factory()() as session:
        upload, error = _scoped_upload(session, upload_id)
        if error is not None:
            return error
        path = Path(upload.storage_key)
        if not path.exists():
            return jsonify({"error": "Knowledge upload file not found."}), 404
        return send_file(
            path,
            mimetype=upload.mime_type,
            as_attachment=True,
            download_name=upload.file_name,
        )


@api_bp.post("/knowledge-uploads/<int:upload_id>/retry")
def retry_company_knowledge_upload_via_api(upload_id: int):
    if not api_can_write():
        return forbidden()
    with get_session_factory()() as session:
        upload, error = _scoped_upload(session, upload_id)
        if error is not None:
            return error
        try:
            retry_upload(
                session=session,
                upload=upload,
                changed_by=_api_changed_by(),
                analysis_options=receipt_analysis_options(
                    llm_purpose=PURPOSE_KNOWLEDGE, session=session
                ),
            )
        except CompanyKnowledgeError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_upload_response(upload)), 202


def _package_from_payload(payload: dict[str, Any]) -> Any:
    if "package" in payload:
        return payload["package"]
    encoded = payload.get("content_base64")
    if not isinstance(encoded, str) or not encoded.strip():
        raise CompanyKnowledgeError("package (JSON-Objekt) oder content_base64 angeben.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        return json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanyKnowledgeError(
            "content_base64 ist kein Base64-kodiertes JSON in UTF-8."
        ) from exc


@api_bp.post("/companies/<int:company_id>/knowledge/import")
def import_company_knowledge_via_api(company_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON body required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company, error = _knowledge_company(session, company_id)
        if error is not None:
            return error
        dry_run = bool_payload(payload.get("dry_run"))
        try:
            report = import_package(
                session=session,
                company=company,
                data=_package_from_payload(payload),
                changed_by=_api_changed_by(),
                dry_run=dry_run,
            )
        except CompanyKnowledgeError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 422
        if dry_run:
            session.rollback()
        else:
            session.commit()
        return jsonify(report), 200 if dry_run else 201


@api_bp.get("/companies/<int:company_id>/knowledge/mcp")
def get_company_knowledge_mcp_via_api(company_id: int):
    """Ob das Firmenwissen der Gesellschaft für MCP-Clients freigegeben ist."""
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        return jsonify({"company_id": company.id, "enabled": company.knowledge_mcp_enabled})


@api_bp.post("/companies/<int:company_id>/knowledge/mcp")
def set_company_knowledge_mcp_via_api(company_id: int):
    """Firmenwissen für MCP-Clients freigeben oder sperren (nur Administratoren, nicht über
    MCP selbst)."""
    user = current_api_user()
    if mcp_channel() or (user is not None and user["role"] != ROLE_ADMIN):
        return forbidden()
    payload = request.get_json(silent=True) or {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean."}), 400
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        set_knowledge_mcp(
            session=session, company=company, enabled=enabled, changed_by=_api_changed_by()
        )
        session.commit()
        return jsonify({"company_id": company.id, "enabled": company.knowledge_mcp_enabled})
