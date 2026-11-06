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
from app.services.company_master_data import partner_party, seller_party
from app.services.document_categories import find_document_category_by_name
from app.services.documents import document_file_metadata
from app.services.einvoice_export import (
    EInvoiceExportError,
    InvoiceLine,
    OutgoingInvoice,
    Party,
    build_einvoice,
)
from app.services.einvoice_import import (
    EInvoiceParseError,
    RateAssignment,
    import_einvoice_booking,
    parse_einvoice,
    preview_einvoice_payload,
)
from app.services.incoming_invoice import IncomingInvoiceError, tax_splits_audit
from app.services.journal_entries import JournalEntryCreationError
from app.services.scoping import scoped_select
from app.services.standard_accounts import VERBINDLICHKEITEN_LUL, standard_account_id
from app.web.blueprint import main_bp
from app.web.formatting import format_amount
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, BusinessPartner, ControllingUnit, Document, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError


@main_bp.get("/erechnung")
def einvoice_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        partners = []
        expense_accounts = []
        creditor_accounts = []
        tax_codes = []
        cost_centers = []
        profit_centers = []
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
            controlling_units = (
                session.execute(
                    scoped_select(ControllingUnit, company_id=selected_company_id)
                    .where(ControllingUnit.is_active.is_(True))
                    .order_by(ControllingUnit.code)
                )
                .scalars()
                .all()
            )
            cost_centers = [u for u in controlling_units if u.unit_type == "cost_center"]
            profit_centers = [u for u in controlling_units if u.unit_type == "profit_center"]
            partners = active_partners(session, selected_company_id)

    return render_template(
        "erechnung.html",
        partners=partners,
        companies=companies,
        selected_company_id=selected_company_id,
        expense_accounts=expense_accounts,
        creditor_accounts=creditor_accounts,
        default_creditor_account_id=standard_account_id(
            creditor_accounts, VERBINDLICHKEITEN_LUL
        ),
        tax_codes=tax_codes,
        rate_tax_codes=_rate_tax_codes(tax_codes),
        cost_centers=cost_centers,
        profit_centers=profit_centers,
        today=date.today().isoformat(),
    )


def _rate_tax_codes(tax_codes: list[TaxCode]) -> list[tuple[Decimal, list[TaxCode]]]:
    """Vorsteuer-Codes gruppiert nach Satz für die Auswahl je Steuersatz."""
    grouped: dict[Decimal, list[TaxCode]] = {}
    for tax_code in tax_codes:
        if tax_code.kind == "input" and tax_code.rate > 0:
            grouped.setdefault(tax_code.rate, []).append(tax_code)
    return sorted(grouped.items())


def active_partners(session, company_id: int) -> list[BusinessPartner]:
    return (
        session.execute(
            scoped_select(BusinessPartner, company_id=company_id)
            .where(BusinessPartner.is_active.is_(True))
            .order_by(BusinessPartner.name)
        )
        .scalars()
        .all()
    )


def _form_rate_assignments(form) -> list[RateAssignment]:
    """Steuercode je Steuersatz aus der Maske (leer = automatisch)."""
    assignments = []
    for rate, tax_code_id in zip(
        form.getlist("rate_tax_rate"), form.getlist("rate_tax_code_id"), strict=False
    ):
        if tax_code_id.strip():
            assignments.append(
                RateAssignment(rate=Decimal(rate.strip()), tax_code_id=int(tax_code_id))
            )
    return assignments


@main_bp.post("/erechnung/pruefen")
def einvoice_preview_action():
    """Prüfung nach der Dateiauswahl (von app.js aufgerufen): Rechnung und Partnererkennung."""
    company_id = request.form.get("company_id", type=int)
    uploaded_file = request.files.get("einvoice_xml")
    if not company_id or uploaded_file is None or not uploaded_file.filename:
        return (
            render_template("_erechnung_pruefung.html", error="Bitte eine XML-Datei wählen."),
            400,
        )
    try:
        invoice = parse_einvoice(uploaded_file.read())
    except EInvoiceParseError as exc:
        return render_template("_erechnung_pruefung.html", error=str(exc)), 422

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        preview = preview_einvoice_payload(session, company.id, invoice)
        return render_template(
            "_erechnung_pruefung.html",
            invoice=invoice,
            partners=active_partners(session, company.id),
            company_id=company.id,
            **preview,
        )


@main_bp.post("/erechnung/buchen")
def einvoice_book_action():
    company_id = request.form.get("company_id", type=int)
    # ``auto`` (ohne Prüfung per JavaScript): nur ein eindeutiger Treffer wird zugeordnet.
    # Leer heißt nach der Prüfung: bewusst ohne Partner.
    partner_choice = request.form.get("partner_id", "auto").strip()
    expense_account_id = request.form.get("expense_account_id", type=int)
    creditor_account_id = request.form.get("creditor_account_id", type=int)
    tax_code_id = request.form.get("tax_code_id", type=int)
    cost_center_id = request.form.get("cost_center_id", type=int)
    profit_center_id = request.form.get("profit_center_id", type=int)
    uploaded_file = request.files.get("einvoice_xml")
    try:
        assignments = _form_rate_assignments(request.form)
    except (ArithmeticError, ValueError):
        flash("Ungültiger Steuersatz in der Zuordnung je Steuersatz.", "error")
        return redirect(url_for("main.einvoice_page", company_id=company_id))

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

        try:
            booking = import_einvoice_booking(
                session=session,
                company=company,
                invoice=invoice,
                expense_account_id=expense_account_id,
                creditor_account_id=creditor_account_id,
                tax_code_id=tax_code_id,
                assignments=assignments,
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
                partner_id=int(partner_choice) if partner_choice.isdigit() else None,
                auto_partner=partner_choice == "auto",
                create_open_item=not request.form.get("skip_open_item"),
                changed_by=changed_by(),
            )
            entry, tax_splits = booking.entry, booking.tax_splits
        except IncomingInvoiceError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.einvoice_page", company_id=company_id))
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

        invoice_category = find_document_category_by_name(
            session=session, company_id=company.id, name="Rechnung"
        )
        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            journal_entry_id=entry.id,
            category_id=invoice_category.id if invoice_category else None,
            file_name=safe_name,
            storage_key=str(target_path),
            mime_type=uploaded_file.mimetype or "application/xml",
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            document_date=invoice.issue_date,
            capture_source="einvoice",
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
                "tax_lines": tax_splits_audit(tax_splits),
                "document_id": document.id,
                "document_date": document.document_date.isoformat(),
                "cost_center_id": cost_center_id,
                "profit_center_id": profit_center_id,
                "partner_id": booking.partner_id,
                "partner_match_status": booking.partner_match.status,
                "open_item_id": booking.open_item.id if booking.open_item else None,
            },
        )
        session.commit()
        posting_number = entry.posting_number
        partner = session.get(BusinessPartner, booking.partner_id) if booking.partner_id else None
        partner_note = (
            f" Partner: {partner.name}."
            if partner is not None
            else f" Ohne Partner: {booking.partner_match.reason}"
        )
        open_item_note = " Offener Posten angelegt." if booking.open_item is not None else ""

    flash(
        f"E-Rechnung {invoice.invoice_number} verbucht als {posting_number} "
        f"(brutto {format_amount(invoice.grand_total)} {invoice.currency_code})."
        f"{partner_note}{open_item_note}",
        "success",
    )
    for warning in booking.warnings:
        flash(warning, "error")
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
    buyer_partner_id = request.form.get("buyer_partner_id", type=int)

    if (
        not company_id
        or not invoice_number
        or not issue_date_raw
        or not (buyer_name or buyer_partner_id)
    ):
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
        seller = seller_party(company)
        currency = company.currency_code
        buyer = Party(
            name=buyer_name,
            street=request.form.get("buyer_street", "").strip(),
            postal_code=request.form.get("buyer_postal_code", "").strip(),
            city=request.form.get("buyer_city", "").strip(),
            country_code=request.form.get("buyer_country_code", "DE").strip() or "DE",
            vat_id=request.form.get("buyer_vat_id", "").strip(),
        )
        if buyer_partner_id:
            partner = session.get(BusinessPartner, buyer_partner_id)
            if partner is None or partner.company_id != company.id:
                flash("Geschäftspartner nicht gefunden.", "error")
                return redirect(url_for("main.einvoice_page", company_id=company_id))
            buyer = partner_party(partner, overrides=buyer)

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
