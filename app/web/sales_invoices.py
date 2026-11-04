"""Ausgangsrechnungen: Liste, Entwurf, Ausstellen, Storno und Downloads."""

from __future__ import annotations

from datetime import date
from pathlib import Path

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
from sqlalchemy import func, select

from app.services.sales_invoice_pdf import price_text
from app.services.sales_invoices import (
    DEFAULT_UNIT_CODE,
    STATUS_LABELS,
    TAX_TREATMENT_RULES,
    UNIT_LABELS,
    SalesInvoiceError,
    cancel_sales_invoice,
    create_sales_invoice,
    delete_sales_invoice,
    einvoice_hints,
    issue_problems,
    issue_sales_invoice,
    list_sales_invoices,
    load_customers,
    payment_status,
    update_sales_invoice,
)
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    pagination_args,
    require_company_access,
    sort_args,
)
from domain.models import (
    SALES_INVOICE_DRAFT,
    SALES_INVOICE_STATUSES,
    SUBLEDGER_DEBTOR,
    TAX_KIND_OUTPUT,
    TAX_TREATMENT_SMALL_BUSINESS,
    TAX_TREATMENT_STANDARD,
    Account,
    BusinessPartner,
    Company,
    SalesInvoice,
    TaxCode,
)

# Vorbelegung einer neuen Position: Erlöskonto 19 % (SKR03/SKR04) und Regelsteuersatz.
DEFAULT_REVENUE_CODES = ("8400", "4400")
DEFAULT_TAX_CODE = "USt19"
PAYMENT_LABELS = {
    "open": "offen",
    "partially_paid": "teilbezahlt",
    "paid": "bezahlt",
}
_HEADER_FIELDS = (
    "partner_id",
    "issue_date",
    "service_date",
    "service_period_from",
    "service_period_to",
    "due_date",
    "currency_code",
    "exchange_rate",
    "buyer_reference",
    "tax_treatment",
    "einvoice_syntax",
    "header_text",
    "footer_text",
    "receivable_account_id",
)


SORT_COLUMNS = ("number", "date", "customer", "status", "due", "net", "gross")


def _sort_expressions(key: str) -> tuple:
    if key == "number":
        return (SalesInvoice.invoice_number,)
    if key == "customer":
        return (
            select(func.lower(BusinessPartner.name))
            .where(BusinessPartner.id == SalesInvoice.partner_id)
            .scalar_subquery(),
        )
    if key == "status":
        return (SalesInvoice.status,)
    if key == "due":
        return (SalesInvoice.due_date,)
    if key == "net":
        return (SalesInvoice.net_total,)
    if key == "gross":
        return (SalesInvoice.gross_total,)
    return (SalesInvoice.issue_date,)


def _upload_dir() -> Path:
    return Path(current_app.config["DOCUMENT_UPLOAD_DIR"])


def _load_invoice(session, invoice_id: int) -> SalesInvoice:
    invoice = session.get(SalesInvoice, invoice_id)
    if invoice is None:
        abort(404)
    require_company_access(session, invoice.company_id)
    return invoice


def _form_decimal(raw: str) -> str:
    """Zahl aus dem Formular, deutsch (``1.234,56``) oder technisch (``1234.56``)."""
    text = (raw or "").strip().replace(" ", "").replace("€", "")
    return text.replace(".", "").replace(",", ".") if "," in text else text


def _form_lines() -> list[dict[str, str]]:
    """Positionen aus dem Formular, unverändert als Text; leere Zeilen entfallen."""
    columns = {
        "description": request.form.getlist("line_description"),
        "quantity": request.form.getlist("line_quantity"),
        "unit_code": request.form.getlist("line_unit_code"),
        "unit_price": request.form.getlist("line_unit_price"),
        "tax_code_id": request.form.getlist("line_tax_code_id"),
        "revenue_account_id": request.form.getlist("line_revenue_account_id"),
    }
    count = max((len(values) for values in columns.values()), default=0)
    rows = []
    for index in range(count):
        row = {
            name: values[index] if index < len(values) else "" for name, values in columns.items()
        }
        if any((row[name] or "").strip() for name in ("description", "quantity", "unit_price")):
            rows.append(row)
    return rows


def _form_fields() -> dict[str, object]:
    fields: dict[str, object] = {
        field: request.form[field] for field in _HEADER_FIELDS if field in request.form
    }
    fields["lines"] = [
        {
            **row,
            "quantity": _form_decimal(row["quantity"]),
            "unit_price": _form_decimal(row["unit_price"]),
        }
        for row in _form_lines()
    ]
    return fields


def _form_choices(session, company: Company) -> dict[str, object]:
    accounts = (
        session.execute(
            scoped_select(Account, company_id=company.id)
            .where(Account.is_active.is_(True))
            .order_by(Account.code)
        )
        .scalars()
        .all()
    )
    tax_codes = (
        session.execute(
            scoped_select(TaxCode, company_id=company.id)
            .where(TaxCode.is_active.is_(True), TaxCode.kind == TAX_KIND_OUTPUT)
            .order_by(TaxCode.code)
        )
        .scalars()
        .all()
    )
    revenue_accounts = [
        account
        for account in accounts
        if account.account_type in {"revenue", "income"} and account.subledger is None
    ]
    return {
        "customers": load_customers(session=session, company_id=company.id),
        "revenue_accounts": revenue_accounts,
        "receivable_accounts": [
            account for account in accounts if account.subledger == SUBLEDGER_DEBTOR
        ],
        "tax_codes": tax_codes,
        "units": UNIT_LABELS,
        "treatments": TAX_TREATMENT_RULES,
        "default_line": {
            "description": "",
            "quantity": "1",
            "unit_code": DEFAULT_UNIT_CODE,
            "unit_price": "",
            "tax_code_id": next(
                (code.id for code in tax_codes if code.code == DEFAULT_TAX_CODE), ""
            )
            if not company.is_small_business
            else "",
            "revenue_account_id": next(
                (acc.id for acc in revenue_accounts if acc.code in DEFAULT_REVENUE_CODES), ""
            ),
        },
    }


def _invoice_form_values(invoice: SalesInvoice) -> tuple[dict[str, object], list[dict]]:
    values = {
        "partner_id": invoice.partner_id,
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else "",
        "service_date": invoice.service_date.isoformat() if invoice.service_date else "",
        "service_period_from": (
            invoice.service_period_from.isoformat() if invoice.service_period_from else ""
        ),
        "service_period_to": (
            invoice.service_period_to.isoformat() if invoice.service_period_to else ""
        ),
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
        "currency_code": invoice.currency_code,
        "exchange_rate": str(invoice.exchange_rate) if invoice.exchange_rate else "",
        "buyer_reference": invoice.buyer_reference or "",
        "tax_treatment": invoice.tax_treatment,
        "einvoice_syntax": invoice.einvoice_syntax,
        "header_text": invoice.header_text or "",
        "footer_text": invoice.footer_text or "",
        "receivable_account_id": invoice.receivable_account_id or "",
    }
    lines = [
        {
            "description": line.description,
            "quantity": format(line.quantity.normalize(), "f"),
            "unit_code": line.unit_code,
            "unit_price": format(line.unit_price.normalize(), "f"),
            "tax_code_id": line.tax_code_id or "",
            "revenue_account_id": line.revenue_account_id or "",
        }
        for line in invoice.lines
    ]
    return values, lines


def _posted_form_values() -> tuple[dict[str, object], list[dict]]:
    values: dict[str, object] = {field: request.form.get(field, "") for field in _HEADER_FIELDS}
    if values["partner_id"]:
        values["partner_id"] = request.form.get("partner_id", type=int) or ""
    if values["receivable_account_id"]:
        values["receivable_account_id"] = (
            request.form.get("receivable_account_id", type=int) or ""
        )
    lines = [
        {
            **row,
            "tax_code_id": int(row["tax_code_id"]) if row["tax_code_id"].isdigit() else "",
            "revenue_account_id": (
                int(row["revenue_account_id"]) if row["revenue_account_id"].isdigit() else ""
            ),
        }
        for row in _form_lines()
    ]
    return values, lines


def _render_form(session, company: Company, *, invoice, values, lines, companies=None):
    problems = hints = []
    if invoice is not None:
        problems = issue_problems(session=session, invoice=invoice)
        hints = einvoice_hints(session=session, invoice=invoice)
    if companies is None:
        companies, _ = company_context(session)
    return render_template(
        "rechnung_entwurf.html",
        companies=companies,
        selected_company_id=company.id,
        company=company,
        invoice=invoice,
        values=values,
        lines=lines,
        problems=problems,
        hints=hints,
        **_form_choices(session, company),
    )


@main_bp.get("/rechnungen")
def sales_invoices_page():
    limit, offset = pagination_args()
    sort = sort_args(SORT_COLUMNS, "date", descending=True)
    status = (request.args.get("status") or "").strip() or None
    if status not in SALES_INVOICE_STATUSES:
        status = None
    partner_id = request.args.get("partner_id", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        invoices: list[SalesInvoice] = []
        total = 0
        customers: list[BusinessPartner] = []
        if selected_company_id:
            invoices, total = list_sales_invoices(
                session=session,
                company_id=selected_company_id,
                status=status,
                partner_id=partner_id,
                limit=limit,
                offset=offset,
                order_by=sort.order_by(*_sort_expressions(sort.key), tiebreak=(SalesInvoice.id,)),
            )
            customers = load_customers(session=session, company_id=selected_company_id)
        filter_args = {
            key: value
            for key, value in (("status", status), ("partner_id", partner_id))
            if value is not None
        }
        return render_template(
            "rechnungen.html",
            companies=companies,
            selected_company_id=selected_company_id,
            invoices=invoices,
            total=total,
            limit=limit,
            offset=offset,
            status=status,
            partner_id=partner_id,
            customers=customers,
            filter_args=filter_args,
            sort=sort,
            status_labels=STATUS_LABELS,
            payment_labels=PAYMENT_LABELS,
            payment_status=payment_status,
        )


@main_bp.get("/rechnungen/neu")
def new_sales_invoice_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        if not selected_company_id:
            flash("Bitte zuerst unter Verwaltung eine Gesellschaft anlegen.", "error")
            return redirect(url_for("main.sales_invoices_page"))
        company = session.get(Company, selected_company_id)
        partner_id = request.args.get("partner_id", type=int)
        partner = session.get(BusinessPartner, partner_id) if partner_id else None
        values = {
            "partner_id": partner.id if partner and partner.company_id == company.id else "",
            "issue_date": date.today().isoformat(),
            "tax_treatment": (
                TAX_TREATMENT_SMALL_BUSINESS
                if company.is_small_business
                else TAX_TREATMENT_STANDARD
            ),
            "einvoice_syntax": "ubl",
        }
        return _render_form(
            session, company, invoice=None, values=values, lines=[], companies=companies
        )


@main_bp.post("/rechnungen")
def create_sales_invoice_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            invoice = create_sales_invoice(
                session=session, company=company, changed_by=changed_by(), **_form_fields()
            )
        except SalesInvoiceError as exc:
            session.rollback()
            flash(str(exc), "error")
            values, lines = _posted_form_values()
            return _render_form(session, company, invoice=None, values=values, lines=lines)
        invoice_id = invoice.id
    flash("Entwurf gespeichert.", "success")
    return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))


@main_bp.get("/rechnungen/<int:invoice_id>")
def sales_invoice_detail_page(invoice_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _load_invoice(session, invoice_id)
        company = session.get(Company, invoice.company_id)
        if invoice.status == SALES_INVOICE_DRAFT:
            values, lines = _invoice_form_values(invoice)
            return _render_form(session, company, invoice=invoice, values=values, lines=lines)
        companies, _ = company_context(session)
        accounts = {
            account.id: account
            for account in session.execute(
                select(Account).where(
                    Account.id.in_([line.revenue_account_id for line in invoice.lines])
                )
            ).scalars()
        }
        return render_template(
            "rechnung_detail.html",
            companies=companies,
            selected_company_id=company.id,
            company=company,
            invoice=invoice,
            accounts=accounts,
            units=UNIT_LABELS,
            price_text=price_text,
            status_labels=STATUS_LABELS,
            payment_labels=PAYMENT_LABELS,
            payment_status=payment_status(invoice),
            treatment=TAX_TREATMENT_RULES[invoice.tax_treatment],
            today=date.today().isoformat(),
        )


@main_bp.post("/rechnungen/<int:invoice_id>")
def update_sales_invoice_action(invoice_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _load_invoice(session, invoice_id)
        company = session.get(Company, invoice.company_id)
        try:
            update_sales_invoice(
                session=session, invoice=invoice, changed_by=changed_by(), **_form_fields()
            )
        except SalesInvoiceError as exc:
            session.rollback()
            flash(str(exc), "error")
            if invoice.status != SALES_INVOICE_DRAFT:
                return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))
            values, lines = _posted_form_values()
            return _render_form(session, company, invoice=invoice, values=values, lines=lines)
    flash("Entwurf gespeichert.", "success")
    return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))


@main_bp.post("/rechnungen/<int:invoice_id>/loeschen")
def delete_sales_invoice_action(invoice_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _load_invoice(session, invoice_id)
        company_id = invoice.company_id
        try:
            delete_sales_invoice(session=session, invoice=invoice, changed_by=changed_by())
        except SalesInvoiceError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))
    flash("Entwurf gelöscht.", "success")
    return redirect(url_for("main.sales_invoices_page", company_id=company_id))


@main_bp.post("/rechnungen/<int:invoice_id>/ausstellen")
def issue_sales_invoice_action(invoice_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        _load_invoice(session, invoice_id)
        try:
            invoice = issue_sales_invoice(
                session=session,
                invoice_id=invoice_id,
                changed_by=changed_by(),
                upload_dir=_upload_dir(),
            )
        except SalesInvoiceError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))
        number = invoice.invoice_number
    flash(f"Rechnung {number} wurde ausgestellt und gebucht.", "success")
    return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))


@main_bp.post("/rechnungen/<int:invoice_id>/stornieren")
def cancel_sales_invoice_action(invoice_id: int):
    raw_date = request.form.get("cancellation_date", "").strip()
    try:
        cancellation_date = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        flash("Ungültiges Stornodatum.", "error")
        return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))
    session_factory = get_session_factory()
    with session_factory() as session:
        _load_invoice(session, invoice_id)
        try:
            cancellation = cancel_sales_invoice(
                session=session,
                invoice_id=invoice_id,
                changed_by=changed_by(),
                upload_dir=_upload_dir(),
                cancellation_date=cancellation_date,
                reason=request.form.get("reason"),
            )
        except SalesInvoiceError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.sales_invoice_detail_page", invoice_id=invoice_id))
        number, cancellation_id = cancellation.invoice_number, cancellation.id
    flash(f"Stornorechnung {number} wurde ausgestellt.", "success")
    return redirect(url_for("main.sales_invoice_detail_page", invoice_id=cancellation_id))


def _send_invoice_document(invoice_id: int, attribute: str):
    session_factory = get_session_factory()
    with session_factory() as session:
        invoice = _load_invoice(session, invoice_id)
        document = getattr(invoice, attribute)
        if document is None or not Path(document.storage_key).exists():
            abort(404)
        return send_file(
            Path(document.storage_key),
            mimetype=document.mime_type,
            as_attachment=True,
            download_name=document.file_name,
        )


@main_bp.get("/rechnungen/<int:invoice_id>/pdf")
def download_sales_invoice_pdf(invoice_id: int):
    return _send_invoice_document(invoice_id, "pdf_document")


@main_bp.get("/rechnungen/<int:invoice_id>/xml")
def download_sales_invoice_xml(invoice_id: int):
    return _send_invoice_document(invoice_id, "xml_document")
