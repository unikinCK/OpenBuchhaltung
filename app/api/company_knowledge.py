"""Firmenwissen je Gesellschaft (ADR-004) über die API: Einträge, Quellen, Paketimport."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    bool_arg,
    bool_payload,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.company_knowledge import (
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
    set_source_active,
    source_dict,
    update_entry,
)

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


def _scoped_entry(session, entry_id: int):
    entry = get_entry(session=session, entry_id=entry_id)
    if entry is None or api_scoped_company(session, entry.company_id) is None:
        return None
    return entry


def _scoped_source(session, source_id: int):
    source = get_source(session=session, source_id=source_id)
    if source is None or api_scoped_company(session, source.company_id) is None:
        return None
    return source


@api_bp.get("/companies/<int:company_id>/knowledge/entries")
def list_company_knowledge_entries_via_api(company_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
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
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
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
        entry = _scoped_entry(session, entry_id)
        if entry is None:
            return jsonify({"error": "Knowledge entry not found."}), 404
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
        entry = _scoped_entry(session, entry_id)
        if entry is None:
            return jsonify({"error": "Knowledge entry not found."}), 404
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
        entry = _scoped_entry(session, entry_id)
        if entry is None:
            return jsonify({"error": "Knowledge entry not found."}), 404
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
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
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
        source = _scoped_source(session, source_id)
        if source is None:
            return jsonify({"error": "Knowledge source not found."}), 404
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
        source = _scoped_source(session, source_id)
        if source is None:
            return jsonify({"error": "Knowledge source not found."}), 404
        set_source_active(
            session=session, source=source, is_active=is_active, changed_by=_api_changed_by()
        )
        session.commit()
        return jsonify(source_dict(source))


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
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
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
