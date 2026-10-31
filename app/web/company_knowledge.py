"""UI für das Firmenwissen je Gesellschaft (ADR-004): Einträge, Quellen, Paketimport."""

from __future__ import annotations

import json

from flask import abort, flash, redirect, render_template, request, url_for

from app.auth import WRITE_ROLES, current_user
from app.services.company_knowledge import (
    AUTHORITY_LABELS,
    KIND_LABELS,
    STATUS_LABELS,
    CompanyKnowledgeError,
    company_knowledge_summary,
    create_entry,
    get_entry,
    get_source,
    import_package,
    knowledge_history,
    list_entries,
    list_sources,
    parse_date_arg,
    set_entry_status,
    set_source_active,
    update_entry,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)

STATUS_FILTERS = (
    ("active", "Alle (ohne ausgemusterte)"),
    ("draft", "Entwürfe"),
    ("approved", "Freigegeben"),
    ("retired", "Ausgemustert"),
    ("all", "Alle"),
)


def _can_write() -> bool:
    user = current_user()
    return bool(user and user["role"] in WRITE_ROLES)


def _form_value(name: str) -> str | None:
    value = request.form.get(name)
    return value if value is not None and value.strip() else None


def _common_context() -> dict:
    return {
        "status_labels": STATUS_LABELS,
        "kind_labels": KIND_LABELS,
        "authority_labels": AUTHORITY_LABELS,
        "can_write": _can_write(),
    }


@main_bp.get("/firmenwissen")
def company_knowledge_page():
    view = request.args.get("view", "entries")
    status = request.args.get("status", "active")
    due_only = request.args.get("due") == "1"
    query = (request.args.get("q") or "").strip()
    with get_session_factory()() as session:
        companies, selected_company_id = company_context(session)
        summary = None
        entries = []
        total = 0
        sources = []
        if selected_company_id:
            summary = company_knowledge_summary(session=session, company_id=selected_company_id)
            sources = list_sources(session=session, company_id=selected_company_id)
            if view != "sources":
                try:
                    entries, total = list_entries(
                        session=session,
                        company_id=selected_company_id,
                        status=status,
                        due_only=due_only,
                        query=query or None,
                        limit=500,
                    )
                except CompanyKnowledgeError as exc:
                    flash(str(exc), "error")
            entries = [
                {
                    "entry": entry,
                    "source_title": entry.source.title if entry.source else None,
                }
                for entry in entries
            ]
        return render_template(
            "firmenwissen.html",
            companies=companies,
            selected_company_id=selected_company_id,
            view=view,
            status=status,
            status_filters=STATUS_FILTERS,
            due_only=due_only,
            query=query,
            summary=summary,
            entries=entries,
            total=total,
            sources=sources,
            report=None,
            **_common_context(),
        )


@main_bp.post("/firmenwissen/eintraege")
def create_company_knowledge_entry_from_form():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    with get_session_factory()() as session:
        company = require_company_access(session, company_id)
        try:
            entry = create_entry(
                session=session,
                company=company,
                title=request.form.get("title"),
                content=request.form.get("content"),
                kind=_form_value("kind"),
                topic=_form_value("topic"),
                source_id=request.form.get("source_id", type=int),
                locator=_form_value("locator"),
                review_due=parse_date_arg(_form_value("review_due"), "Wiedervorlage"),
                review_note=_form_value("review_note"),
                changed_by=changed_by(),
            )
            session.commit()
            entry_id = entry.id
        except CompanyKnowledgeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.company_knowledge_page", company_id=company_id))
    flash("Eintrag wurde als Entwurf angelegt.", "success")
    return redirect(url_for("main.company_knowledge_entry_page", entry_id=entry_id))


def _load_entry(session, entry_id: int):
    entry = get_entry(session=session, entry_id=entry_id)
    if entry is None:
        abort(404)
    require_company_access(session, entry.company_id)
    return entry


@main_bp.get("/firmenwissen/eintraege/<int:entry_id>")
def company_knowledge_entry_page(entry_id: int):
    with get_session_factory()() as session:
        entry = _load_entry(session, entry_id)
        companies, _selected = company_context(session)
        history = knowledge_history(
            session=session,
            company_id=entry.company_id,
            entity_type="company_knowledge_entry",
            entity_id=entry.id,
        )
        sources = list_sources(session=session, company_id=entry.company_id)
        return render_template(
            "firmenwissen_eintrag.html",
            companies=companies,
            selected_company_id=entry.company_id,
            entry=entry,
            source=entry.source,
            sources=sources,
            history=history,
            **_common_context(),
        )


@main_bp.post("/firmenwissen/eintraege/<int:entry_id>/aendern")
def update_company_knowledge_entry_from_form(entry_id: int):
    with get_session_factory()() as session:
        entry = _load_entry(session, entry_id)
        try:
            source_raw = request.form.get("source_id", "")
            changed = update_entry(
                session=session,
                entry=entry,
                changed_by=changed_by(),
                title=request.form.get("title"),
                content=request.form.get("content"),
                kind=_form_value("kind") or entry.kind,
                topic=_form_value("topic"),
                source_id=int(source_raw) if source_raw.strip() else None,
                locator=_form_value("locator"),
                review_due=parse_date_arg(_form_value("review_due"), "Wiedervorlage"),
                review_note=_form_value("review_note"),
            )
            status = entry.status
            session.commit()
        except (CompanyKnowledgeError, ValueError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.company_knowledge_entry_page", entry_id=entry_id))
    if not changed:
        flash("Keine Änderung erkannt.", "success")
    elif status == "draft":
        flash("Eintrag wurde geändert; er ist ein Entwurf und braucht eine Freigabe.", "success")
    else:
        flash("Eintrag wurde geändert.", "success")
    return redirect(url_for("main.company_knowledge_entry_page", entry_id=entry_id))


@main_bp.post("/firmenwissen/eintraege/<int:entry_id>/status")
def set_company_knowledge_entry_status_from_form(entry_id: int):
    with get_session_factory()() as session:
        entry = _load_entry(session, entry_id)
        try:
            set_entry_status(
                session=session,
                entry=entry,
                status=request.form.get("status", ""),
                changed_by=changed_by(),
            )
            label = STATUS_LABELS[entry.status]
            session.commit()
        except CompanyKnowledgeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.company_knowledge_entry_page", entry_id=entry_id))
    flash(f"Status: {label}.", "success")
    return redirect(url_for("main.company_knowledge_entry_page", entry_id=entry_id))


@main_bp.get("/firmenwissen/quellen/<int:source_id>")
def company_knowledge_source_page(source_id: int):
    with get_session_factory()() as session:
        source = get_source(session=session, source_id=source_id)
        if source is None:
            abort(404)
        require_company_access(session, source.company_id)
        companies, _selected = company_context(session)
        entries, _total = list_entries(
            session=session, company_id=source.company_id, status="all", source_id=source.id
        )
        history = knowledge_history(
            session=session,
            company_id=source.company_id,
            entity_type="company_knowledge_source",
            entity_id=source.id,
        )
        return render_template(
            "firmenwissen_quelle.html",
            companies=companies,
            selected_company_id=source.company_id,
            source=source,
            excerpts=list(source.excerpts),
            entries=entries,
            history=history,
            **_common_context(),
        )


@main_bp.post("/firmenwissen/quellen/<int:source_id>/aktiv")
def set_company_knowledge_source_active_from_form(source_id: int):
    with get_session_factory()() as session:
        source = get_source(session=session, source_id=source_id)
        if source is None:
            abort(404)
        require_company_access(session, source.company_id)
        is_active = request.form.get("is_active") == "true"
        set_source_active(
            session=session, source=source, is_active=is_active, changed_by=changed_by()
        )
        session.commit()
    flash("Quelle wurde aktiviert." if is_active else "Quelle wurde deaktiviert.", "success")
    return redirect(url_for("main.company_knowledge_source_page", source_id=source_id))


@main_bp.post("/firmenwissen/import")
def import_company_knowledge_from_form():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    dry_run = request.form.get("dry_run") == "1"
    upload = request.files.get("package_file")
    if upload is None or not upload.filename:
        flash("Bitte eine Paketdatei (JSON) auswählen.", "error")
        return redirect(url_for("main.company_knowledge_page", company_id=company_id))
    try:
        data = json.loads(upload.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        flash("Die Datei ist kein JSON in UTF-8.", "error")
        return redirect(url_for("main.company_knowledge_page", company_id=company_id))
    with get_session_factory()() as session:
        company = require_company_access(session, company_id)
        try:
            report = import_package(
                session=session,
                company=company,
                data=data,
                changed_by=changed_by(),
                dry_run=dry_run,
            )
        except CompanyKnowledgeError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.company_knowledge_page", company_id=company_id))
        if dry_run:
            session.rollback()
        else:
            session.commit()
        companies, selected_company_id = company_context(session)
        summary = company_knowledge_summary(session=session, company_id=company_id)
        sources = list_sources(session=session, company_id=company_id)
        return render_template(
            "firmenwissen.html",
            companies=companies,
            selected_company_id=company_id,
            view="import",
            status="active",
            status_filters=STATUS_FILTERS,
            due_only=False,
            query="",
            summary=summary,
            entries=[],
            total=0,
            sources=sources,
            report=report,
            **_common_context(),
        )
