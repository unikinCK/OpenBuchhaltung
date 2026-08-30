"""Buchungsvorlagen und Eröffnungsbilanz über die API."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.journal_entries import JournalEntryCreationError
from app.services.journal_templates import (
    JournalTemplateError,
    book_template,
    create_template,
    list_templates,
    serialize_template,
    set_template_active,
)
from app.services.opening_balance import OpeningBalanceError, book_opening_balance
from domain.models import JournalTemplate
from domain.services.journal_entry_validation import JournalEntryValidationError


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


@api_bp.get("/journal-templates")
def list_journal_templates_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    include_inactive = (request.args.get("include_inactive") or "").lower() in {"1", "true"}

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        templates = list_templates(
            session=session, company_id=company_id, include_inactive=include_inactive
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "templates": [serialize_template(t) for t in templates],
                }
            ),
            200,
        )


@api_bp.post("/journal-templates")
def create_journal_template_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    next_run = None
    if payload.get("next_run"):
        try:
            next_run = date.fromisoformat(str(payload["next_run"]))
        except ValueError:
            return jsonify({"error": "next_run must be an ISO date."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            template = create_template(
                session=session,
                company_id=company_id,
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                lines=payload.get("lines") or [],
                interval=str(payload.get("interval") or "on_demand"),
                next_run=next_run,
                changed_by=_api_changed_by(),
            )
        except JournalTemplateError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_template(template)), 201


@api_bp.post("/journal-templates/<int:template_id>/active")
def set_journal_template_active_via_api(template_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    is_active = payload.get("is_active")
    if not isinstance(is_active, bool):
        return jsonify({"error": "is_active (boolean) is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        template = session.get(JournalTemplate, template_id)
        if template is None or api_scoped_company(session, template.company_id) is None:
            return jsonify({"error": "Template not found."}), 404
        template = set_template_active(
            session=session,
            template_id=template_id,
            is_active=is_active,
            changed_by=_api_changed_by(),
        )
        return jsonify(serialize_template(template)), 200


@api_bp.post("/journal-templates/<int:template_id>/book")
def book_journal_template_via_api(template_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    entry_date = None
    if payload.get("entry_date"):
        try:
            entry_date = date.fromisoformat(str(payload["entry_date"]))
        except ValueError:
            return jsonify({"error": "entry_date must be an ISO date."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        template = session.get(JournalTemplate, template_id)
        if template is None or api_scoped_company(session, template.company_id) is None:
            return jsonify({"error": "Template not found."}), 404
        try:
            entry, template = book_template(
                session=session,
                template_id=template_id,
                entry_date=entry_date,
                changed_by=_api_changed_by(),
            )
        except (
            JournalTemplateError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            return jsonify({"error": str(exc)}), 422
        return (
            jsonify(
                {
                    "journal_entry_id": entry.id,
                    "posting_number": entry.posting_number,
                    "entry_date": entry.entry_date.isoformat(),
                    "template": serialize_template(template),
                }
            ),
            201,
        )


@api_bp.post("/opening-balance")
def book_opening_balance_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        entry_date = date.fromisoformat(str(payload.get("entry_date")))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and entry_date are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            entry = book_opening_balance(
                session=session,
                company_id=company_id,
                entry_date=entry_date,
                balances=payload.get("balances") or [],
                changed_by=_api_changed_by(),
            )
        except (
            OpeningBalanceError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            return jsonify({"error": str(exc)}), 422
        return (
            jsonify(
                {
                    "journal_entry_id": entry.id,
                    "posting_number": entry.posting_number,
                    "entry_date": entry.entry_date.isoformat(),
                }
            ),
            201,
        )
