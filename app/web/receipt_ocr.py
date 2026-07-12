"""Beleg-OCR: Upload mit Analyse und Buchung des Eingangsrechnungs-Vorschlags."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from flask import current_app, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.receipt_ocr import (
    ReceiptExtraction,
    ReceiptOCRError,
    analyze_document,
)
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    document_upload_error,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, Document, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError


def _receipt_account_context(session, company_id: int):
    """Konten- und Steuercode-Listen für die OCR-Buchungsmaske."""
    accounts = (
        session.execute(
            scoped_select(Account, company_id=company_id).order_by(Account.code)
        )
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
    return expense_accounts, creditor_accounts, tax_codes


def _render_receipt_ocr_page(
    *,
    extraction: ReceiptExtraction | None = None,
    document_id: int | None = None,
    document_name: str | None = None,
):
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        expense_accounts: list = []
        creditor_accounts: list = []
        tax_codes: list = []
        if selected_company_id:
            expense_accounts, creditor_accounts, tax_codes = _receipt_account_context(
                session, selected_company_id
            )
    return render_template(
        "belege_ocr.html",
        companies=companies,
        selected_company_id=selected_company_id,
        expense_accounts=expense_accounts,
        creditor_accounts=creditor_accounts,
        tax_codes=tax_codes,
        extraction=extraction,
        document_id=document_id,
        document_name=document_name,
        today=date.today().isoformat(),
    )


@main_bp.get("/belege/ocr")
def receipt_ocr_page():
    return _render_receipt_ocr_page()


@main_bp.post("/belege/ocr/vorschlag")
def receipt_ocr_suggest():
    company_id = request.form.get("company_id", type=int)
    uploaded_file = request.files.get("document_file")

    if not company_id or uploaded_file is None or not uploaded_file.filename:
        flash("Gesellschaft und Datei sind Pflichtfelder.", "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    original_file_name = secure_filename(uploaded_file.filename)
    if not original_file_name:
        flash("Ungültiger Dateiname.", "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    upload_error = document_upload_error(uploaded_file, original_file_name)
    if upload_error:
        flash(upload_error, "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    file_bytes = uploaded_file.read()
    mime_type = uploaded_file.mimetype or "application/octet-stream"

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)

        unique_name = f"{uuid4().hex}_{original_file_name}"
        company_dir = (
            Path(current_app.config["DOCUMENT_UPLOAD_DIR"])
            / str(company.tenant_id)
            / str(company.id)
        )
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        target_path.write_bytes(file_bytes)

        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name=original_file_name,
            storage_key=str(target_path),
            mime_type=mime_type,
        )
        session.add(document)
        session.flush()
        document_id = document.id
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document.id),
            action="uploaded",
            changed_by=changed_by(),
            payload={"file_name": document.file_name, "mime_type": document.mime_type},
        )
        session.commit()

    try:
        extraction = analyze_document(
            file_bytes=file_bytes,
            mime_type=mime_type,
            file_name=original_file_name,
            ocr_endpoint=current_app.config.get("RECEIPT_OCR_ENDPOINT_URL"),
            ocr_model=current_app.config.get("RECEIPT_OCR_MODEL", "gpt-4.1-mini"),
            llm_endpoint=current_app.config.get("RECEIPT_LLM_ENDPOINT_URL"),
            llm_model=current_app.config.get("RECEIPT_LLM_MODEL", "gpt-4.1-mini"),
        )
    except ReceiptOCRError as exc:
        flash(f"OCR nicht möglich: {exc}", "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    with session_factory() as session:
        company = require_company_access(session, company_id)
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document_id),
            action="ocr_analyzed",
            changed_by=changed_by(),
            payload={
                "source": extraction.source,
                "confidence": extraction.confidence,
                "llm_used": extraction.llm_used,
                "control_status": extraction.control_status,
                "gross_amount": str(extraction.gross_amount)
                if extraction.gross_amount is not None
                else None,
                "tax_rate": str(extraction.tax_rate)
                if extraction.tax_rate is not None
                else None,
                "supplier": extraction.supplier,
                "invoice_number": extraction.invoice_number,
            },
        )
        session.commit()

    if not extraction.has_booking_basis:
        flash(
            "Es konnte kein Betrag erkannt werden. Der Beleg ist gespeichert; "
            "die Buchung bitte manuell erfassen.",
            "error",
        )
    return _render_receipt_ocr_page(
        extraction=extraction,
        document_id=document_id,
        document_name=original_file_name,
    )


@main_bp.post("/belege/ocr/buchen")
def receipt_ocr_book():
    company_id = request.form.get("company_id", type=int)
    document_id = request.form.get("document_id", type=int)
    expense_account_id = request.form.get("expense_account_id", type=int)
    creditor_account_id = request.form.get("creditor_account_id", type=int)
    tax_code_id = request.form.get("tax_code_id", type=int)
    entry_date_raw = request.form.get("entry_date", "").strip()
    description = request.form.get("description", "").strip()

    try:
        net_amount = parse_decimal(request.form.get("net_amount") or "0")
        tax_amount = parse_decimal(request.form.get("tax_amount") or "0")
    except (JournalEntryCreationError, ValueError):
        flash("Netto- und Steuerbetrag müssen gültige Zahlen sein.", "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    if not company_id or not expense_account_id or not creditor_account_id:
        flash("Gesellschaft sowie Aufwands- und Kreditorenkonto sind Pflicht.", "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    try:
        entry_date = date.fromisoformat(entry_date_raw) if entry_date_raw else date.today()
    except ValueError:
        flash("Ungültiges Buchungsdatum.", "error")
        return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

    zero = Decimal("0.00")
    gross_amount = (net_amount + tax_amount).quantize(zero)

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)

        document = session.get(Document, document_id) if document_id else None
        if document is None or document.company_id != company.id:
            flash("Zugehöriger Beleg wurde nicht gefunden.", "error")
            return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

        expense_account = session.get(Account, expense_account_id)
        creditor_account = session.get(Account, creditor_account_id)
        for account in (expense_account, creditor_account):
            if account is None or account.company_id != company.id:
                flash("Ausgewähltes Konto gehört nicht zur Gesellschaft.", "error")
                return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

        lines = [
            JournalLineInput(
                account_id=expense_account.id,
                debit_amount=net_amount,
                credit_amount=zero,
                description=description or None,
            )
        ]
        if tax_amount > zero:
            tax_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
            if tax_code is None or tax_code.company_id != company.id:
                flash("Für die Steuer bitte einen gültigen Steuercode wählen.", "error")
                return redirect(url_for("main.receipt_ocr_page", company_id=company_id))
            if tax_code.vat_account_id is None:
                flash(f"Steuercode {tax_code.code} hat kein Steuerkonto.", "error")
                return redirect(url_for("main.receipt_ocr_page", company_id=company_id))
            lines.append(
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    debit_amount=tax_amount,
                    credit_amount=zero,
                    description=f"Vorsteuer ({tax_code.code})",
                )
            )
        lines.append(
            JournalLineInput(
                account_id=creditor_account.id,
                debit_amount=zero,
                credit_amount=gross_amount,
            )
        )

        try:
            entry = create_journal_entry(
                session=session,
                payload=JournalEntryInput(
                    company_id=company.id,
                    entry_date=entry_date,
                    description=description or "Belegbuchung (OCR)",
                    status="posted",
                    changed_by=changed_by(),
                    lines=lines,
                ),
            )
        except (JournalEntryCreationError, JournalEntryValidationError) as exc:
            flash(f"Buchung fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.receipt_ocr_page", company_id=company_id))

        document.journal_entry_id = entry.id
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="document",
            entity_id=str(document.id),
            action="ocr_booked",
            changed_by=changed_by(),
            payload={
                "journal_entry_id": entry.id,
                "net_amount": str(net_amount),
                "tax_amount": str(tax_amount),
                "gross_amount": str(gross_amount),
            },
        )
        session.commit()
        posting_number = entry.posting_number

    flash(
        f"Beleg verbucht als {posting_number} (brutto {gross_amount} EUR) "
        "und mit der Buchung verknüpft.",
        "success",
    )
    return redirect(url_for("main.journal_page", company_id=company_id))
