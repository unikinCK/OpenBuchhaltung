"""E-Rechnung: Import (XRechnung/ZUGFeRD) mit Buchung sowie Export."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from flask import (
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from app.services.audit_log import log_audit_event
from app.services.documents import document_file_metadata
from app.services.einvoice_export import (
    EInvoiceExportError,
    InvoiceLine,
    OutgoingInvoice,
    Party,
    build_einvoice,
)
from app.services.einvoice_import import EInvoiceParseError, parse_einvoice
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, Document, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError


@main_bp.get("/erechnung")
def einvoice_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        expense_accounts = []
        creditor_accounts = []
        tax_codes = []
        if selected_company_id:
            accounts = (
                session.execute(
                    scoped_select(Account, company_id=selected_company_id).order_by(Account.code)
                )
                .scalars()
                .all()
            )
            expense_accounts = [a for a in accounts if a.account_type == "expense"]
            creditor_accounts = [
                a for a in accounts if a.account_type in {"liability", "asset"}
            ]
            tax_codes = (
                session.execute(
                    scoped_select(TaxCode, company_id=selected_company_id)
                    .where(TaxCode.is_active.is_(True))
                    .order_by(TaxCode.code)
                )
                .scalars()
                .all()
            )

    return render_template(
        "erechnung.html",
        companies=companies,
        selected_company_id=selected_company_id,
        expense_accounts=expense_accounts,
        creditor_accounts=creditor_accounts,
        tax_codes=tax_codes,
        today=date.today().isoformat(),
    )


@main_bp.post("/erechnung/buchen")
def einvoice_book_action():
    company_id = request.form.get("company_id", type=int)
    expense_account_id = request.form.get("expense_account_id", type=int)
    creditor_account_id = request.form.get("creditor_account_id", type=int)
    tax_code_id = request.form.get("tax_code_id", type=int)
    uploaded_file = request.files.get("einvoice_xml")

    if (
        not company_id
        or not expense_account_id
        or not creditor_account_id
        or uploaded_file is None
        or not uploaded_file.filename
    ):
        flash("Gesellschaft, Aufwands- und Kreditorenkonto sowie XML-Datei sind Pflicht.", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    xml_bytes = uploaded_file.read()
    try:
        invoice = parse_einvoice(xml_bytes)
    except EInvoiceParseError as exc:
        flash(f"E-Rechnung konnte nicht gelesen werden: {exc}", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)

        expense_account = session.get(Account, expense_account_id)
        creditor_account = session.get(Account, creditor_account_id)
        for account in (expense_account, creditor_account):
            if account is None or account.company_id != company.id:
                flash("Ausgewähltes Konto gehört nicht zur Gesellschaft.", "error")
                return redirect(url_for("main.einvoice_page", company_id=company_id))

        zero = Decimal("0.00")
        lines = [
            JournalLineInput(
                account_id=expense_account.id,
                debit_amount=invoice.net_total,
                credit_amount=zero,
                description=f"{invoice.seller_name} {invoice.invoice_number}".strip(),
            )
        ]
        if invoice.tax_total > zero:
            tax_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
            if tax_code is None or tax_code.company_id != company.id:
                flash("Für die Steuer bitte einen gültigen Steuercode wählen.", "error")
                return redirect(url_for("main.einvoice_page", company_id=company_id))
            if tax_code.vat_account_id is None:
                flash(f"Steuercode {tax_code.code} hat kein Steuerkonto.", "error")
                return redirect(url_for("main.einvoice_page", company_id=company_id))
            # Steuer wird exakt aus der Rechnung übernommen; keine Auto-Expansion
            # über tax_code_id, damit der Bruttobetrag exakt aufgeht.
            lines.append(
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    debit_amount=invoice.tax_total,
                    credit_amount=zero,
                    description=f"Steuer {invoice.primary_tax_rate}%",
                )
            )
        lines.append(
            JournalLineInput(
                account_id=creditor_account.id,
                debit_amount=zero,
                credit_amount=invoice.grand_total,
            )
        )

        try:
            entry = create_journal_entry(
                session=session,
                payload=JournalEntryInput(
                    company_id=company.id,
                    entry_date=invoice.issue_date,
                    description=(
                        f"E-Rechnung {invoice.invoice_number} ({invoice.seller_name})"
                    ).strip(),
                    status="posted",
                    changed_by=changed_by(),
                    lines=lines,
                ),
            )
        except (JournalEntryCreationError, JournalEntryValidationError) as exc:
            flash(f"Buchung fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.einvoice_page", company_id=company_id))

        # XML als Beleg speichern und mit der Buchung verknüpfen.
        safe_name = secure_filename(uploaded_file.filename) or "erechnung.xml"
        unique_name = f"{uuid4().hex}_{safe_name}"
        company_dir = (
            Path(current_app.config["DOCUMENT_UPLOAD_DIR"])
            / str(company.tenant_id)
            / str(company.id)
        )
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        target_path.write_bytes(xml_bytes)
        metadata = document_file_metadata(xml_bytes)

        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            journal_entry_id=entry.id,
            file_name=safe_name,
            storage_key=str(target_path),
            mime_type=uploaded_file.mimetype or "application/xml",
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
        )
        session.add(document)
        session.flush()
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="einvoice",
            entity_id=str(entry.id),
            action="imported",
            changed_by=changed_by(),
            payload={
                "invoice_number": invoice.invoice_number,
                "seller": invoice.seller_name,
                "syntax": invoice.syntax,
                "grand_total": str(invoice.grand_total),
                "document_id": document.id,
            },
        )
        session.commit()
        posting_number = entry.posting_number

    flash(
        f"E-Rechnung {invoice.invoice_number} verbucht als {posting_number} "
        f"(brutto {invoice.grand_total} {invoice.currency_code}).",
        "success",
    )
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/erechnung/export")
def einvoice_export_action():
    company_id = request.form.get("company_id", type=int)
    syntax = request.form.get("syntax", "ubl").strip().lower()
    if syntax not in {"ubl", "cii"}:
        syntax = "ubl"

    invoice_number = request.form.get("invoice_number", "").strip()
    issue_date_raw = request.form.get("issue_date", "").strip()
    buyer_name = request.form.get("buyer_name", "").strip()

    if not company_id or not invoice_number or not issue_date_raw or not buyer_name:
        flash("Gesellschaft, Rechnungsnummer, Datum und Käufer sind Pflichtfelder.", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    try:
        issue_date = date.fromisoformat(issue_date_raw)
    except ValueError:
        flash("Ungültiges Rechnungsdatum.", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    names = request.form.getlist("line_name")
    quantities = request.form.getlist("line_quantity")
    prices = request.form.getlist("line_unit_price")
    rates = request.form.getlist("line_tax_rate")

    lines: list[InvoiceLine] = []
    try:
        for idx in range(max(len(names), len(quantities), len(prices))):
            name = (names[idx] if idx < len(names) else "").strip()
            qty_raw = (quantities[idx] if idx < len(quantities) else "").strip()
            price_raw = (prices[idx] if idx < len(prices) else "").strip()
            rate_raw = (rates[idx] if idx < len(rates) else "").strip()
            if not name and not qty_raw and not price_raw:
                continue
            if not name or not qty_raw or not price_raw:
                flash(
                    f"Position {idx + 1}: Bezeichnung, Menge und Preis sind erforderlich.",
                    "error",
                )
                return redirect(url_for("main.einvoice_page", company_id=company_id))
            lines.append(
                InvoiceLine(
                    name=name,
                    quantity=Decimal(qty_raw),
                    unit_price=Decimal(price_raw),
                    tax_rate=Decimal(rate_raw or "0"),
                )
            )
    except (ArithmeticError, ValueError):
        flash("Menge, Preis oder Steuersatz sind keine gültigen Zahlen.", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    if not lines:
        flash("Bitte mindestens eine Rechnungsposition erfassen.", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        seller = Party(
            name=company.name,
            street=current_app.config.get("SELLER_STREET", ""),
            postal_code=current_app.config.get("SELLER_POSTAL_CODE", ""),
            city=current_app.config.get("SELLER_CITY", ""),
            country_code=current_app.config.get("SELLER_COUNTRY_CODE", "DE"),
            vat_id=current_app.config.get("SELLER_VAT_ID", ""),
        )
        currency = company.currency_code

    buyer = Party(
        name=buyer_name,
        street=request.form.get("buyer_street", "").strip(),
        postal_code=request.form.get("buyer_postal_code", "").strip(),
        city=request.form.get("buyer_city", "").strip(),
        country_code=request.form.get("buyer_country_code", "DE").strip() or "DE",
        vat_id=request.form.get("buyer_vat_id", "").strip(),
    )

    try:
        invoice = OutgoingInvoice(
            invoice_number=invoice_number,
            issue_date=issue_date,
            seller=seller,
            buyer=buyer,
            lines=lines,
            currency_code=currency,
            buyer_reference=request.form.get("buyer_reference", "").strip(),
        )
        xml = build_einvoice(invoice, syntax=syntax)
    except EInvoiceExportError as exc:
        flash(f"E-Rechnung konnte nicht erzeugt werden: {exc}", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

    safe_number = secure_filename(invoice_number) or "rechnung"
    file_name = f"XRechnung_{syntax}_{safe_number}.xml"
    response = make_response(xml)
    response.headers["Content-Type"] = "application/xml; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return response
