"""Belegabgleich-UI: Belege mit Buchungen abgleichen, Vorschläge freigeben/ablehnen."""

from __future__ import annotations

from datetime import date

from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app.services.journal_entries import JournalEntryCreationError, parse_decimal
from app.services.receipt_matching import (
    STATUS_OPEN,
    ReceiptMatchError,
    approve_match_suggestion,
    book_new_booking_suggestion,
    create_match_suggestion,
    entry_gross_total,
    reject_suggestion,
)
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import (
    Account,
    ControllingUnit,
    Document,
    JournalEntry,
    ReceiptMatchSuggestion,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError


def _booking_form_context(session, company_id: int):
    """Konten-, Steuercode- und Controlling-Listen für die Freigabemasken."""
    accounts = (
        session.execute(scoped_select(Account, company_id=company_id).order_by(Account.code))
        .scalars()
        .all()
    )
    expense_accounts = [a for a in accounts if a.account_type == "expense"]
    creditor_accounts = [a for a in accounts if a.account_type in {"liability", "asset"}]
    tax_codes = (
        session.execute(
            scoped_select(TaxCode, company_id=company_id)
            .where(TaxCode.is_active.is_(True))
            .order_by(TaxCode.code)
        )
        .scalars()
        .all()
    )
    controlling_units = (
        session.execute(
            scoped_select(ControllingUnit, company_id=company_id)
            .where(ControllingUnit.is_active.is_(True))
            .order_by(ControllingUnit.code)
        )
        .scalars()
        .all()
    )
    cost_centers = [u for u in controlling_units if u.unit_type == "cost_center"]
    profit_centers = [u for u in controlling_units if u.unit_type == "profit_center"]
    return expense_accounts, creditor_accounts, tax_codes, cost_centers, profit_centers


def _unlinked_entries(session, company_id: int, limit: int = 100) -> list[JournalEntry]:
    """Buchungen ohne Belegverknüpfung (Auswahlliste für die Freigabe)."""
    linked = select(Document.journal_entry_id).where(Document.journal_entry_id.is_not(None))
    return (
        session.execute(
            select(JournalEntry)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.id.not_in(linked),
                JournalEntry.reversal_of_id.is_(None),
            )
            .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


@main_bp.get("/belege/abgleich")
def receipt_matching_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        unmatched_documents: list[Document] = []
        open_suggestions: list[ReceiptMatchSuggestion] = []
        decided_suggestions: list[ReceiptMatchSuggestion] = []
        entry_options: list[JournalEntry] = []
        entries_by_id: dict[int, JournalEntry] = {}
        documents_by_id: dict[int, Document] = {}
        expense_accounts: list = []
        creditor_accounts: list = []
        tax_codes: list = []
        cost_centers: list = []
        profit_centers: list = []

        if selected_company_id:
            open_document_ids = select(ReceiptMatchSuggestion.document_id).where(
                ReceiptMatchSuggestion.company_id == selected_company_id,
                ReceiptMatchSuggestion.status == STATUS_OPEN,
            )
            unmatched_documents = (
                session.execute(
                    scoped_select(Document, company_id=selected_company_id)
                    .where(
                        Document.journal_entry_id.is_(None),
                        Document.is_current.is_(True),
                        Document.id.not_in(open_document_ids),
                    )
                    .order_by(Document.uploaded_at.desc())
                    .limit(50)
                )
                .scalars()
                .all()
            )
            open_suggestions = (
                session.execute(
                    scoped_select(ReceiptMatchSuggestion, company_id=selected_company_id)
                    .where(ReceiptMatchSuggestion.status == STATUS_OPEN)
                    .order_by(ReceiptMatchSuggestion.id.desc())
                )
                .scalars()
                .all()
            )
            decided_suggestions = (
                session.execute(
                    scoped_select(ReceiptMatchSuggestion, company_id=selected_company_id)
                    .where(ReceiptMatchSuggestion.status != STATUS_OPEN)
                    .order_by(ReceiptMatchSuggestion.id.desc())
                    .limit(20)
                )
                .scalars()
                .all()
            )
            entry_options = _unlinked_entries(session, selected_company_id)
            entries_by_id = {entry.id: entry for entry in entry_options}
            for suggestion in open_suggestions + decided_suggestions:
                if suggestion.journal_entry_id and suggestion.journal_entry_id not in entries_by_id:
                    entry = session.get(JournalEntry, suggestion.journal_entry_id)
                    if entry is not None:
                        entries_by_id[entry.id] = entry
                document = session.get(Document, suggestion.document_id)
                if document is not None:
                    documents_by_id[document.id] = document
            (
                expense_accounts,
                creditor_accounts,
                tax_codes,
                cost_centers,
                profit_centers,
            ) = _booking_form_context(session, selected_company_id)

        return render_template(
            "belege_abgleich.html",
            companies=companies,
            selected_company_id=selected_company_id,
            unmatched_documents=unmatched_documents,
            open_suggestions=open_suggestions,
            decided_suggestions=decided_suggestions,
            entry_options=entry_options,
            entries_by_id=entries_by_id,
            documents_by_id=documents_by_id,
            entry_gross_total=entry_gross_total,
            expense_accounts=expense_accounts,
            creditor_accounts=creditor_accounts,
            tax_codes=tax_codes,
            cost_centers=cost_centers,
            profit_centers=profit_centers,
            today=date.today().isoformat(),
        )


@main_bp.post("/belege/abgleich/vorschlag")
def receipt_matching_suggest():
    company_id = request.form.get("company_id", type=int)
    document_id = request.form.get("document_id", type=int)
    if not company_id or not document_id:
        flash("Gesellschaft und Beleg sind Pflichtfelder.", "error")
        return redirect(url_for("main.receipt_matching_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            suggestion = create_match_suggestion(
                session=session,
                company_id=company.id,
                document_id=document_id,
                changed_by=changed_by(),
                ocr_endpoint=current_app.config.get("RECEIPT_OCR_ENDPOINT_URL"),
                ocr_model=current_app.config.get("RECEIPT_OCR_MODEL", "gpt-4.1-mini"),
                llm_endpoint=current_app.config.get("RECEIPT_MATCH_LLM_ENDPOINT_URL"),
                llm_model=current_app.config.get("RECEIPT_MATCH_LLM_MODEL", "gpt-4.1-mini"),
            )
        except ReceiptMatchError as exc:
            flash(f"Abgleich nicht möglich: {exc}", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

        if suggestion.suggestion_type == "match":
            flash(
                "Passende Buchung gefunden – bitte den Vorschlag prüfen und freigeben.",
                "success",
            )
        else:
            flash(
                "Keine passende Buchung gefunden – es liegt ein Vorschlag für eine "
                "neue Buchung zur Freigabe bereit.",
                "success",
            )
    return redirect(url_for("main.receipt_matching_page", company_id=company_id))


@main_bp.post("/belege/abgleich/<int:suggestion_id>/freigeben")
def receipt_matching_approve(suggestion_id: int):
    company_id = request.form.get("company_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id) if company_id else None
        suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
        if company is None or suggestion is None or suggestion.company_id != company.id:
            flash("Abgleichsvorschlag nicht gefunden.", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

        try:
            suggestion = approve_match_suggestion(
                session=session,
                suggestion_id=suggestion.id,
                changed_by=changed_by(),
                journal_entry_id=journal_entry_id,
            )
        except ReceiptMatchError as exc:
            flash(f"Freigabe fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

        entry = session.get(JournalEntry, suggestion.journal_entry_id)
        posting_number = entry.posting_number if entry else suggestion.journal_entry_id

    flash(f"Beleg freigegeben und mit Buchung {posting_number} verknüpft.", "success")
    return redirect(url_for("main.receipt_matching_page", company_id=company_id))


@main_bp.post("/belege/abgleich/<int:suggestion_id>/buchen")
def receipt_matching_book(suggestion_id: int):
    company_id = request.form.get("company_id", type=int)
    expense_account_id = request.form.get("expense_account_id", type=int)
    creditor_account_id = request.form.get("creditor_account_id", type=int)
    tax_code_id = request.form.get("tax_code_id", type=int)
    cost_center_id = request.form.get("cost_center_id", type=int)
    profit_center_id = request.form.get("profit_center_id", type=int)
    entry_date_raw = request.form.get("entry_date", "").strip()
    description = request.form.get("description", "").strip()

    if not company_id or not expense_account_id or not creditor_account_id:
        flash("Gesellschaft sowie Aufwands- und Kreditorenkonto sind Pflicht.", "error")
        return redirect(url_for("main.receipt_matching_page", company_id=company_id))

    try:
        net_amount = parse_decimal(request.form.get("net_amount") or "0")
        tax_amount = parse_decimal(request.form.get("tax_amount") or "0")
    except (JournalEntryCreationError, ValueError):
        flash("Netto- und Steuerbetrag müssen gültige Zahlen sein.", "error")
        return redirect(url_for("main.receipt_matching_page", company_id=company_id))

    try:
        entry_date = date.fromisoformat(entry_date_raw) if entry_date_raw else None
    except ValueError:
        flash("Ungültiges Buchungsdatum.", "error")
        return redirect(url_for("main.receipt_matching_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
        if suggestion is None or suggestion.company_id != company.id:
            flash("Abgleichsvorschlag nicht gefunden.", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

        try:
            suggestion, entry = book_new_booking_suggestion(
                session=session,
                suggestion_id=suggestion.id,
                changed_by=changed_by(),
                expense_account_id=expense_account_id,
                creditor_account_id=creditor_account_id,
                net_amount=net_amount,
                tax_amount=tax_amount,
                tax_code_id=tax_code_id,
                entry_date=entry_date,
                description=description or None,
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
            )
        except (
            ReceiptMatchError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            flash(f"Buchung fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

        posting_number = entry.posting_number

    flash(
        f"Buchung {posting_number} aus dem Belegvorschlag erzeugt und "
        "mit dem Beleg verknüpft.",
        "success",
    )
    return redirect(url_for("main.receipt_matching_page", company_id=company_id))


@main_bp.post("/belege/abgleich/<int:suggestion_id>/ablehnen")
def receipt_matching_reject(suggestion_id: int):
    company_id = request.form.get("company_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id) if company_id else None
        suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
        if company is None or suggestion is None or suggestion.company_id != company.id:
            flash("Abgleichsvorschlag nicht gefunden.", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

        try:
            reject_suggestion(
                session=session, suggestion_id=suggestion.id, changed_by=changed_by()
            )
        except ReceiptMatchError as exc:
            flash(f"Ablehnen fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.receipt_matching_page", company_id=company_id))

    flash("Abgleichsvorschlag abgelehnt – der Beleg bleibt unverknüpft.", "success")
    return redirect(url_for("main.receipt_matching_page", company_id=company_id))
