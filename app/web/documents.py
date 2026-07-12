"""Belegverwaltung: Upload, Verknüpfung mit Buchungen und Download."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

from app.auth import current_tenant_id
from app.services.audit_log import log_audit_event
from app.services.document_llm import DocumentLLMError, send_document_update
from app.services.documents import document_file_metadata
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    document_upload_error,
    get_session_factory,
    require_company_access,
)
from domain.models import Document, JournalEntry


@main_bp.get("/belege")
def documents_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        documents = []
        journal_entries = []
        if selected_company_id:
            documents = (
                session.execute(
                    scoped_select(Document, company_id=selected_company_id).order_by(
                        Document.uploaded_at.desc()
                    )
                )
                .scalars()
                .all()
            )
            journal_entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                )
                .scalars()
                .all()
            )
        journal_entry_labels = {entry.id: entry.posting_number for entry in journal_entries}

    return render_template(
        "belege.html",
        companies=companies,
        selected_company_id=selected_company_id,
        documents=documents,
        journal_entries=journal_entries,
        journal_entry_labels=journal_entry_labels,
    )


@main_bp.post("/documents")
def upload_document():
    company_id = request.form.get("company_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)
    uploaded_file = request.files.get("document_file")

    if not company_id or uploaded_file is None or not uploaded_file.filename:
        flash("Gesellschaft und Datei sind Pflichtfelder.", "error")
        return redirect(url_for("main.documents_page", company_id=company_id))

    original_file_name = secure_filename(uploaded_file.filename)
    if not original_file_name:
        flash("Ungültiger Dateiname.", "error")
        return redirect(url_for("main.documents_page", company_id=company_id))

    upload_error = document_upload_error(uploaded_file, original_file_name)
    if upload_error:
        flash(upload_error, "error")
        return redirect(url_for("main.documents_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)

        linked_entry = None
        if journal_entry_id is not None:
            linked_entry = session.get(JournalEntry, journal_entry_id)
            if linked_entry is None or linked_entry.company_id != company.id:
                flash("Ausgewählte Buchung wurde nicht gefunden.", "error")
                return redirect(url_for("main.documents_page", company_id=company_id))

        unique_name = f"{uuid4().hex}_{original_file_name}"
        tenant_dir = Path(current_app.config["DOCUMENT_UPLOAD_DIR"]) / str(company.tenant_id)
        company_dir = tenant_dir / str(company.id)
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        file_bytes = uploaded_file.read()
        target_path.write_bytes(file_bytes)
        metadata = document_file_metadata(file_bytes)

        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            journal_entry_id=linked_entry.id if linked_entry else None,
            file_name=original_file_name,
            storage_key=str(target_path),
            mime_type=uploaded_file.mimetype or "application/octet-stream",
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
        )
        session.add(document)
        session.flush()

        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document.id),
            action="uploaded",
            changed_by=changed_by(),
            payload={
                "file_name": document.file_name,
                "journal_entry_id": document.journal_entry_id,
                "mime_type": document.mime_type,
                "file_sha256": document.file_sha256,
                "file_size_bytes": document.file_size_bytes,
                "version_number": document.version_number,
            },
        )
        session.commit()

        uploaded_document_id = document.id
        uploaded_tenant_id = document.tenant_id
        uploaded_company_id = document.company_id
        uploaded_file_name = document.file_name
        uploaded_mime_type = document.mime_type
        uploaded_journal_entry_id = document.journal_entry_id

    llm_endpoint = current_app.config.get("DOCUMENT_LLM_ENDPOINT_URL")
    if llm_endpoint:
        try:
            llm_response = send_document_update(
                endpoint_url=llm_endpoint,
                model=current_app.config.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini"),
                company_id=company_id,
                document_id=uploaded_document_id,
                file_name=uploaded_file_name,
                mime_type=uploaded_mime_type,
                journal_entry_id=uploaded_journal_entry_id,
            )
            with session_factory() as session:
                log_audit_event(
                    session=session,
                    tenant_id=uploaded_tenant_id,
                    company_id=uploaded_company_id,
                    entity_type="document",
                    entity_id=str(uploaded_document_id),
                    action="llm_update_requested",
                    changed_by=changed_by(),
                    payload={"status": "success", "response": llm_response},
                )
                session.commit()
        except DocumentLLMError as exc:
            current_app.logger.warning(
                "LLM request for document %s failed: %s",
                uploaded_document_id,
                exc.message,
            )
            with session_factory() as session:
                log_audit_event(
                    session=session,
                    tenant_id=uploaded_tenant_id,
                    company_id=uploaded_company_id,
                    entity_type="document",
                    entity_id=str(uploaded_document_id),
                    action="llm_update_requested",
                    changed_by=changed_by(),
                    payload={"status": "error", "message": exc.message},
                )
                session.commit()

    flash("Beleg wurde hochgeladen.", "success")
    return redirect(url_for("main.documents_page", company_id=company_id))


@main_bp.post("/documents/<int:document_id>/link")
def link_document(document_id: int):
    company_id = request.form.get("company_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None:
            abort(404)
        require_company_access(session, document.company_id)

        linked_entry = None
        if journal_entry_id is not None:
            linked_entry = session.get(JournalEntry, journal_entry_id)
            if linked_entry is None or linked_entry.company_id != document.company_id:
                flash("Ausgewählte Buchung wurde nicht gefunden.", "error")
                return redirect(
                    url_for("main.documents_page", company_id=company_id or document.company_id)
                )

        previous_journal_entry_id = document.journal_entry_id
        document.journal_entry_id = linked_entry.id if linked_entry else None

        log_audit_event(
            session=session,
            tenant_id=document.tenant_id,
            company_id=document.company_id,
            entity_type="document",
            entity_id=str(document.id),
            action="linked",
            changed_by=changed_by(),
            payload={
                "previous_journal_entry_id": previous_journal_entry_id,
                "journal_entry_id": document.journal_entry_id,
            },
        )
        session.commit()
        redirect_company_id = document.company_id

    if linked_entry:
        flash("Beleg wurde mit der Buchung verknüpft.", "success")
    else:
        flash("Verknüpfung des Belegs wurde entfernt.", "success")
    return redirect(url_for("main.documents_page", company_id=redirect_company_id))


@main_bp.post("/documents/<int:document_id>/replace")
def replace_document(document_id: int):
    uploaded_file = request.files.get("document_file")
    if uploaded_file is None or not uploaded_file.filename:
        flash("Bitte eine neue Belegdatei auswählen.", "error")
        return redirect(url_for("main.documents_page"))

    original_file_name = secure_filename(uploaded_file.filename)
    if not original_file_name:
        flash("Ungültiger Dateiname.", "error")
        return redirect(url_for("main.documents_page"))

    upload_error = document_upload_error(uploaded_file, original_file_name)
    if upload_error:
        flash(upload_error, "error")
        return redirect(url_for("main.documents_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        previous = session.get(Document, document_id)
        if previous is None:
            abort(404)
        require_company_access(session, previous.company_id)
        if not previous.is_current:
            flash("Nur die aktuelle Belegversion kann ersetzt werden.", "error")
            return redirect(url_for("main.documents_page", company_id=previous.company_id))

        unique_name = f"{uuid4().hex}_{original_file_name}"
        company_dir = (
            Path(current_app.config["DOCUMENT_UPLOAD_DIR"])
            / str(previous.tenant_id)
            / str(previous.company_id)
        )
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        file_bytes = uploaded_file.read()
        target_path.write_bytes(file_bytes)
        metadata = document_file_metadata(file_bytes)

        replacement = Document(
            tenant_id=previous.tenant_id,
            company_id=previous.company_id,
            journal_entry_id=previous.journal_entry_id,
            file_name=original_file_name,
            storage_key=str(target_path),
            mime_type=uploaded_file.mimetype or "application/octet-stream",
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            version_number=previous.version_number + 1,
            replaces_document_id=previous.id,
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
            changed_by=changed_by(),
            payload={
                "replacement_document_id": replacement.id,
                "previous_file_sha256": previous.file_sha256,
                "replacement_file_sha256": replacement.file_sha256,
                "previous_version_number": previous.version_number,
                "replacement_version_number": replacement.version_number,
            },
        )
        session.commit()
        company_id = replacement.company_id

    flash("Beleg wurde als neue Version ersetzt.", "success")
    return redirect(url_for("main.documents_page", company_id=company_id))


@main_bp.get("/documents/<int:document_id>/download")
def download_document(document_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None:
            abort(404)
        tenant_scope = current_tenant_id()
        if tenant_scope is not None and document.tenant_id != tenant_scope:
            abort(404)
        document_path = Path(document.storage_key)
        if not document_path.exists():
            abort(404)

        return send_file(
            document_path,
            mimetype=document.mime_type,
            as_attachment=True,
            download_name=document.file_name,
        )
