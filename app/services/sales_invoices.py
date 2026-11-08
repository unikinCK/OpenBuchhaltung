"""Ausgangsrechnungen: Entwurf, Ausstellen und Storno (ADR-006).

Ein Entwurf hat keine Nummer und darf geändert oder gelöscht werden. Das Ausstellen läuft
atomar: Pflichtangaben prüfen, Nummer ziehen, buchen, offenen Posten anlegen, XML und PDF
erzeugen und als Belege ablegen. Scheitert ein Schritt, fällt alles zurück; auch die Nummer,
der Nummernkreis bleibt lückenlos. Ausgestellte Rechnungen sind unveränderlich, korrigiert wird
per Stornorechnung.

Beträge laufen in der Rechnungswährung ``currency_code``, auch in Fremdwährung (ADR-010);
``ledger_amount`` ist die eine Stelle, an der ein Betrag für das Hauptbuch umgerechnet wird.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from werkzeug.utils import secure_filename

from app.services.account_chart_check import detect_company_chart
from app.services.audit_log import log_audit_event
from app.services.company_master_data import SellerData, resolve_seller, seller_party
from app.services.document_categories import find_document_category_by_name
from app.services.documents import CAPTURE_SOURCE_SALES_INVOICE, document_file_metadata
from app.services.einvoice_export import (
    INVOICE_TYPE_COMMERCIAL,
    INVOICE_TYPE_CREDIT_NOTE,
    EInvoiceExportError,
    InvoiceLine,
    OutgoingInvoice,
    Party,
    TaxGroup,
    build_einvoice,
    compute_tax_groups,
)
from app.services.exchange_rates import (
    ExchangeRateError,
    format_rate_text,
    normalize_currency,
    normalize_rate,
    rate_for,
    require_base_currency,
    to_company_amount,
)
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
    reverse_journal_entry,
)
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    close_open_item_by_cancellation,
    create_open_item,
)
from app.services.sales_invoice_pdf import (
    PdfInvoice,
    PdfInvoiceLine,
    build_invoice_pdf,
    embed_xml_attachment,
)
from app.services.vat_returns import EU_COUNTRY_CODES, company_on_cash_basis, revenue_kennzahl
from domain.formatting import format_amount
from domain.models import (
    SALES_INVOICE_CANCELLED,
    SALES_INVOICE_DRAFT,
    SALES_INVOICE_ISSUED,
    SALES_INVOICE_STATUSES,
    SALES_INVOICE_TYPE_CANCELLATION,
    SALES_INVOICE_TYPE_INVOICE,
    SUBLEDGER_DEBTOR,
    TAX_KIND_OUTPUT,
    TAX_TREATMENT_EXEMPT,
    TAX_TREATMENT_EXPORT,
    TAX_TREATMENT_INTRA_COMMUNITY,
    TAX_TREATMENT_REVERSE_CHARGE,
    TAX_TREATMENT_SMALL_BUSINESS,
    TAX_TREATMENT_STANDARD,
    TAX_TREATMENTS,
    Account,
    BusinessPartner,
    Company,
    Document,
    JournalEntry,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceSequence,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
SOURCE_SALES_INVOICE = "sales_invoice"
EINVOICE_SYNTAXES = ("ubl", "cii")
DEFAULT_SYNTAX = "ubl"
DEFAULT_UNIT_CODE = "C62"
# Häufige Mengeneinheiten nach UN/ECE Rec 20; andere Codes sind zulässig.
UNIT_LABELS = {
    "C62": "Stück",
    "HUR": "Stunde",
    "DAY": "Tag",
    "MON": "Monat",
    "LS": "Pauschale",
    "KGM": "kg",
    "MTR": "m",
    "LTR": "l",
    "MTK": "m²",
}
STATUS_LABELS = {
    SALES_INVOICE_DRAFT: "Entwurf",
    SALES_INVOICE_ISSUED: "Ausgestellt",
    SALES_INVOICE_CANCELLED: "Storniert",
}
QUANTITY_PLACES = 4
PRICE_PLACES = 4
DESCRIPTION_LIMIT = 2000
TEXT_LIMIT = 4000
BUYER_REFERENCE_LIMIT = 100
_UNIT_CODE_RE = re.compile(r"^[A-Z0-9]{1,3}$")
_FIELDS = (
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
    "lines",
)
_LINE_FIELDS = (
    "description",
    "quantity",
    "unit_code",
    "unit_price",
    "tax_code_id",
    "revenue_account_id",
)


class SalesInvoiceError(ValueError):
    """Fachlicher Fehler; ``problems`` listet fehlende Pflichtangaben einzeln auf."""

    def __init__(self, message: str, problems: Sequence[str] = ()):
        super().__init__(message)
        self.problems = list(problems)


class SalesInvoiceStateError(SalesInvoiceError):
    """Der Status der Rechnung lässt den Vorgang nicht zu (REST: 409)."""


@dataclass(frozen=True, slots=True)
class TaxTreatmentRule:
    label: str
    category: str  # BT-118/BT-151
    note: str  # Pflichthinweis auf der Rechnung und BT-120
    reason_code: str = ""  # BT-121 (VATEX)
    # Kennzahlen der UStVA, zu denen das Erlöskonto passen sollte, und ein Vorschlag.
    kennzahlen: frozenset[str] = frozenset()
    account_hint: str = ""


# § 14 Abs. 4 Nr. 6a UStG: Istversteuerer weisen ab 2028 auf der Rechnung darauf hin.
CASH_BASIS_NOTE_FROM = date(2028, 1, 1)
CASH_BASIS_NOTE = "Versteuerung nach vereinnahmten Entgelten."

TAX_TREATMENT_RULES = {
    TAX_TREATMENT_STANDARD: TaxTreatmentRule("Regelbesteuerung (Steuerausweis)", "S", ""),
    TAX_TREATMENT_REVERSE_CHARGE: TaxTreatmentRule(
        "Steuerschuldnerschaft des Leistungsempfängers (§ 13b UStG)",
        "AE",
        "Steuerschuldnerschaft des Leistungsempfängers (§ 13b UStG). "
        "Die Umsatzsteuer schuldet der Leistungsempfänger.",
        "VATEX-EU-AE",
        frozenset({"21", "60"}),
        "SKR03 8336/8337, SKR04 4336/4337",
    ),
    TAX_TREATMENT_INTRA_COMMUNITY: TaxTreatmentRule(
        "Innergemeinschaftliche Lieferung (§ 4 Nr. 1b UStG)",
        "K",
        "Steuerfreie innergemeinschaftliche Lieferung nach § 4 Nr. 1b UStG i. V. m. § 6a UStG.",
        "VATEX-EU-IC",
        frozenset({"41", "42", "44"}),
        "SKR03 8125, SKR04 4125",
    ),
    TAX_TREATMENT_EXPORT: TaxTreatmentRule(
        "Ausfuhrlieferung (§ 4 Nr. 1a UStG)",
        "G",
        "Steuerfreie Ausfuhrlieferung nach § 4 Nr. 1a UStG i. V. m. § 6 UStG.",
        "VATEX-EU-G",
        frozenset({"43"}),
        "SKR03 8120, SKR04 4120",
    ),
    TAX_TREATMENT_EXEMPT: TaxTreatmentRule(
        "Steuerfreier Umsatz (§ 4 UStG)",
        "E",
        "Steuerfreier Umsatz nach § 4 UStG.",
        "",
        frozenset({"43", "48"}),
        "SKR03 8150, SKR04 4150",
    ),
    TAX_TREATMENT_SMALL_BUSINESS: TaxTreatmentRule(
        "Kleinunternehmer (§ 19 UStG)",
        "E",
        "Steuerbefreit als Kleinunternehmer nach § 19 UStG; es wird keine Umsatzsteuer "
        "ausgewiesen.",
    ),
}


@dataclass(slots=True)
class InvoiceTotals:
    net: Decimal
    tax: Decimal
    gross: Decimal
    groups: list[TaxGroup]


def compute_invoice_totals(lines: Sequence[SalesInvoiceLine]) -> InvoiceTotals:
    """Summen und Steueraufteilung; die Steuer wird je Satz aus der Summe gerundet."""
    groups = compute_tax_groups((line.tax_rate, line.net_amount) for line in lines)
    net = sum((line.net_amount for line in lines), ZERO).quantize(CENT)
    tax = sum((group.tax_amount for group in groups), ZERO).quantize(CENT)
    return InvoiceTotals(net=net, tax=tax, gross=(net + tax).quantize(CENT), groups=groups)


def invoice_currency(company: Company, requested: Any = None) -> str:
    """Rechnungswährung: die Firmenwährung oder eine Fremdwährung (ADR-010)."""
    try:
        code = normalize_currency(requested or company.currency_code)
        if code != company.currency_code:
            require_base_currency(company)
    except ExchangeRateError as exc:
        raise SalesInvoiceError(str(exc)) from exc
    return code


def ledger_amount(invoice: SalesInvoice, amount: Decimal) -> Decimal:
    """Betrag für Hauptbuch und offenen Posten: umgerechnet, wenn die Rechnung einen Kurs hat."""
    if invoice.exchange_rate is None:
        return amount
    return to_company_amount(amount, invoice.exchange_rate)


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


def _date(value: Any, field: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise SalesInvoiceError(f"{field} muss ein Datum im Format JJJJ-MM-TT sein.") from exc


def _text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SalesInvoiceError(f"{field} muss ein Text sein.")
    normalized = value.strip()
    if len(normalized) > limit:
        raise SalesInvoiceError(f"{field} darf höchstens {limit} Zeichen lang sein.")
    return normalized or None


def _id(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SalesInvoiceError(f"{field} muss eine ID sein.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SalesInvoiceError(f"{field} muss eine ID sein.") from exc


def _decimal(value: Any, field: str, places: int, number: int) -> Decimal:
    try:
        return parse_decimal(str(value), places=places)
    except JournalEntryCreationError as exc:
        raise SalesInvoiceError(f"Position {number}: {field}: {exc}") from exc


def _load_customer(session: Session, company_id: int, partner_id: int | None) -> BusinessPartner:
    if partner_id is None:
        raise SalesInvoiceError("Kunde (partner_id) ist Pflicht.")
    partner = session.get(BusinessPartner, partner_id)
    if partner is None or partner.company_id != company_id:
        raise SalesInvoiceError("Geschäftspartner in dieser Gesellschaft nicht gefunden.")
    if partner.debtor_number is None:
        raise SalesInvoiceError(f"{partner.name} ist kein Kunde (keine Debitorennummer).")
    return partner


def _build_lines(
    session: Session, company: Company, raw_lines: Any
) -> list[SalesInvoiceLine]:
    if not isinstance(raw_lines, (list, tuple)):
        raise SalesInvoiceError("lines muss eine Liste von Positionen sein.")
    lines: list[SalesInvoiceLine] = []
    for number, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, dict):
            raise SalesInvoiceError(f"Position {number} muss ein Objekt sein.")
        unknown = set(raw) - set(_LINE_FIELDS)
        if unknown:
            raise SalesInvoiceError(
                f"Position {number}: unbekannte Felder {', '.join(sorted(unknown))}."
            )
        description = _text(raw.get("description"), "Beschreibung", DESCRIPTION_LIMIT)
        if not description:
            raise SalesInvoiceError(f"Position {number}: Beschreibung ist Pflicht.")
        if raw.get("quantity") in (None, "") or raw.get("unit_price") in (None, ""):
            raise SalesInvoiceError(f"Position {number}: Menge und Einzelpreis sind Pflicht.")
        quantity = _decimal(raw["quantity"], "Menge", QUANTITY_PLACES, number)
        unit_price = _decimal(raw["unit_price"], "Einzelpreis", PRICE_PLACES, number)
        if quantity == 0:
            raise SalesInvoiceError(f"Position {number}: Menge darf nicht 0 sein.")
        if unit_price < 0:
            raise SalesInvoiceError(
                f"Position {number}: Der Einzelpreis darf nicht negativ sein "
                "(EN 16931). Nachlässe als negative Menge erfassen."
            )
        unit_code = str(raw.get("unit_code") or DEFAULT_UNIT_CODE).strip().upper()
        if not _UNIT_CODE_RE.match(unit_code):
            raise SalesInvoiceError(
                f"Position {number}: Einheit {unit_code} ist kein Code nach UN/ECE Rec 20."
            )
        tax_code = _load_tax_code(session, company.id, _id(raw.get("tax_code_id"), "tax_code_id"))
        account = _load_revenue_account(
            session, company.id, _id(raw.get("revenue_account_id"), "revenue_account_id")
        )
        lines.append(
            SalesInvoiceLine(
                tenant_id=company.tenant_id,
                line_number=number,
                description=description,
                quantity=quantity,
                unit_code=unit_code,
                unit_price=unit_price,
                tax_code_id=tax_code.id if tax_code else None,
                revenue_account_id=account.id if account else None,
                tax_rate=tax_code.rate if tax_code else ZERO,
                net_amount=(quantity * unit_price).quantize(CENT, rounding=ROUND_HALF_UP),
            )
        )
    return lines


def _load_tax_code(session: Session, company_id: int, tax_code_id: int | None) -> TaxCode | None:
    if tax_code_id is None:
        return None
    tax_code = session.get(TaxCode, tax_code_id)
    if tax_code is None or tax_code.company_id != company_id:
        raise SalesInvoiceError("Steuercode in dieser Gesellschaft nicht gefunden.")
    if tax_code.kind != TAX_KIND_OUTPUT:
        raise SalesInvoiceError(
            f"Steuercode {tax_code.code} ist ein Vorsteuercode; Rechnungen brauchen einen "
            "Umsatzsteuercode."
        )
    return tax_code


def _load_revenue_account(
    session: Session, company_id: int, account_id: int | None
) -> Account | None:
    if account_id is None:
        return None
    account = session.get(Account, account_id)
    if account is None or account.company_id != company_id:
        raise SalesInvoiceError("Erlöskonto in dieser Gesellschaft nicht gefunden.")
    if account.account_type not in {"revenue", "income"}:
        raise SalesInvoiceError(f"Konto {account.code} ist kein Erlöskonto.")
    return account


def _load_receivable_account(
    session: Session, company_id: int, account_id: int | None
) -> Account | None:
    if account_id is None:
        return None
    account = session.get(Account, account_id)
    if account is None or account.company_id != company_id:
        raise SalesInvoiceError("Debitoren-Sammelkonto in dieser Gesellschaft nicht gefunden.")
    if account.subledger != SUBLEDGER_DEBTOR:
        raise SalesInvoiceError(
            f"Konto {account.code} ist kein Debitoren-Sammelkonto. Das Kennzeichen wird in der "
            "Kontenpflege gesetzt."
        )
    return account


def _apply_fields(
    session: Session, company: Company, invoice: SalesInvoice, fields: dict[str, Any]
) -> None:
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise SalesInvoiceError(f"Unbekannte Felder: {', '.join(sorted(unknown))}.")
    if "partner_id" in fields:
        partner = _load_customer(session, company.id, _id(fields["partner_id"], "partner_id"))
        if not partner.is_active:
            raise SalesInvoiceError(f"{partner.name} ist inaktiv.")
        invoice.partner_id = partner.id
    for field in ("issue_date", "service_date", "service_period_from", "service_period_to",
                  "due_date"):
        if field in fields:
            setattr(invoice, field, _date(fields[field], field))
    previous_currency, previous_rate = invoice.currency_code, invoice.exchange_rate
    if "currency_code" in fields:
        invoice.currency_code = invoice_currency(company, fields["currency_code"])
    if "exchange_rate" in fields:
        try:
            invoice.exchange_rate = (
                normalize_rate(fields["exchange_rate"])
                if fields["exchange_rate"] not in (None, "")
                else None
            )
        except ExchangeRateError as exc:
            raise SalesInvoiceError(str(exc)) from exc
    # Ein Kurs gehört zu seiner Währung: Beim Wechsel ohne neuen Kurs gilt wieder der Tageskurs.
    if invoice.currency_code == company.currency_code or (
        invoice.currency_code != previous_currency and invoice.exchange_rate == previous_rate
    ):
        invoice.exchange_rate = None
    if "buyer_reference" in fields:
        invoice.buyer_reference = _text(
            fields["buyer_reference"], "buyer_reference", BUYER_REFERENCE_LIMIT
        )
    if "tax_treatment" in fields:
        treatment = fields["tax_treatment"]
        if treatment not in TAX_TREATMENTS:
            raise SalesInvoiceError(
                f"tax_treatment muss eines von {', '.join(TAX_TREATMENTS)} sein."
            )
        invoice.tax_treatment = treatment
    if "einvoice_syntax" in fields:
        syntax = str(fields["einvoice_syntax"] or DEFAULT_SYNTAX).strip().lower()
        if syntax not in EINVOICE_SYNTAXES:
            raise SalesInvoiceError("einvoice_syntax muss ubl oder cii sein.")
        invoice.einvoice_syntax = syntax
    for field in ("header_text", "footer_text"):
        if field in fields:
            setattr(invoice, field, _text(fields[field], field, TEXT_LIMIT))
    if "receivable_account_id" in fields:
        account = _load_receivable_account(
            session, company.id, _id(fields["receivable_account_id"], "receivable_account_id")
        )
        invoice.receivable_account_id = account.id if account else None
    if "lines" in fields:
        lines = _build_lines(session, company, fields["lines"])
        # Erst löschen, dann anlegen: sonst kollidieren die Positionsnummern.
        invoice.lines.clear()
        session.flush()
        invoice.lines.extend(lines)
    _set_totals(invoice)


def _set_totals(invoice: SalesInvoice) -> None:
    totals = compute_invoice_totals(invoice.lines)
    invoice.net_total = totals.net
    invoice.tax_total = totals.tax
    invoice.gross_total = totals.gross


# ---------------------------------------------------------------------------
# Lesen und Serialisieren
# ---------------------------------------------------------------------------


def list_sales_invoices(
    *,
    session: Session,
    company_id: int,
    status: str | None = None,
    partner_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    order_by: Sequence[Any] = (),
) -> tuple[list[SalesInvoice], int]:
    """Rechnungen, standardmäßig die jüngsten zuerst."""
    conditions = [SalesInvoice.company_id == company_id]
    if status is not None:
        if status not in SALES_INVOICE_STATUSES:
            raise SalesInvoiceError(
                f"status muss eines von {', '.join(SALES_INVOICE_STATUSES)} sein."
            )
        conditions.append(SalesInvoice.status == status)
    if partner_id is not None:
        conditions.append(SalesInvoice.partner_id == partner_id)
    total = session.execute(
        select(func.count()).select_from(SalesInvoice).where(*conditions)
    ).scalar_one()
    rows = (
        session.execute(
            select(SalesInvoice)
            .options(selectinload(SalesInvoice.partner), selectinload(SalesInvoice.open_item))
            .where(*conditions)
            .order_by(*(order_by or (SalesInvoice.issue_date.desc(), SalesInvoice.id.desc())))
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        .scalars()
        .all()
    )
    return list(rows), total


def payment_status(invoice: SalesInvoice) -> str | None:
    """Zahlstand aus dem offenen Posten: ``open``, ``partially_paid``, ``paid``."""
    item = invoice.open_item
    if item is None or invoice.status != SALES_INVOICE_ISSUED:
        return None
    if invoice.invoice_type == SALES_INVOICE_TYPE_CANCELLATION:
        return None
    if item.open_amount == 0:
        return "paid"
    return "open" if item.open_amount == item.original_amount else "partially_paid"


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_sales_invoice_line(line: SalesInvoiceLine) -> dict[str, Any]:
    return {
        "id": line.id,
        "line_number": line.line_number,
        "description": line.description,
        "quantity": str(line.quantity),
        "unit_code": line.unit_code,
        "unit_price": str(line.unit_price),
        "tax_code_id": line.tax_code_id,
        "revenue_account_id": line.revenue_account_id,
        "tax_rate": str(line.tax_rate),
        "net_amount": str(line.net_amount),
    }


def serialize_sales_invoice(invoice: SalesInvoice, *, with_lines: bool = True) -> dict[str, Any]:
    item = invoice.open_item
    data: dict[str, Any] = {
        "id": invoice.id,
        "tenant_id": invoice.tenant_id,
        "company_id": invoice.company_id,
        "partner_id": invoice.partner_id,
        "partner_name": invoice.partner.name if invoice.partner else None,
        "invoice_number": invoice.invoice_number,
        "status": invoice.status,
        "invoice_type": invoice.invoice_type,
        "cancels_invoice_id": invoice.cancels_invoice_id,
        "issue_date": _iso(invoice.issue_date),
        "service_date": _iso(invoice.service_date),
        "service_period_from": _iso(invoice.service_period_from),
        "service_period_to": _iso(invoice.service_period_to),
        "due_date": _iso(invoice.due_date),
        "currency_code": invoice.currency_code,
        "exchange_rate": str(invoice.exchange_rate) if invoice.exchange_rate else None,
        "recipient_name": invoice.recipient_name,
        "recipient_street": invoice.recipient_street,
        "recipient_postal_code": invoice.recipient_postal_code,
        "recipient_city": invoice.recipient_city,
        "recipient_country_code": invoice.recipient_country_code,
        "recipient_vat_id": invoice.recipient_vat_id,
        "buyer_reference": invoice.buyer_reference,
        "tax_treatment": invoice.tax_treatment,
        "einvoice_syntax": invoice.einvoice_syntax,
        "header_text": invoice.header_text,
        "footer_text": invoice.footer_text,
        "net_total": str(invoice.net_total),
        "tax_total": str(invoice.tax_total),
        "gross_total": str(invoice.gross_total),
        "receivable_account_id": invoice.receivable_account_id,
        "journal_entry_id": invoice.journal_entry_id,
        "open_item_id": invoice.open_item_id,
        "open_amount": str(item.open_amount) if item is not None else None,
        "foreign_open_amount": (
            str(item.foreign_open_amount)
            if item is not None and item.foreign_open_amount is not None
            else None
        ),
        "payment_status": payment_status(invoice),
        "pdf_document_id": invoice.pdf_document_id,
        "xml_document_id": invoice.xml_document_id,
        "created_by": invoice.created_by,
        "created_at": _iso(invoice.created_at),
        "issued_by": invoice.issued_by,
        "issued_at": _iso(invoice.issued_at),
    }
    if with_lines:
        data["lines"] = [serialize_sales_invoice_line(line) for line in invoice.lines]
    return data


def _audit(
    session: Session,
    invoice: SalesInvoice,
    action: str,
    changed_by: str,
    payload: dict[str, Any],
) -> None:
    log_audit_event(
        session=session,
        tenant_id=invoice.tenant_id,
        company_id=invoice.company_id,
        entity_type="sales_invoice",
        entity_id=str(invoice.id),
        action=action,
        changed_by=changed_by,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Entwurf
# ---------------------------------------------------------------------------


def load_customers(*, session: Session, company_id: int) -> list[BusinessPartner]:
    """Aktive Kunden für die Auswahl im Entwurf."""
    return list(
        session.execute(
            select(BusinessPartner)
            .where(
                BusinessPartner.company_id == company_id,
                BusinessPartner.debtor_number.is_not(None),
                BusinessPartner.is_active.is_(True),
            )
            .order_by(BusinessPartner.name)
        ).scalars()
    )


def create_sales_invoice(
    *, session: Session, company: Company, changed_by: str, **fields: Any
) -> SalesInvoice:
    """Legt einen Entwurf an. Kleinunternehmer erhalten ``small_business`` als Vorgabe."""
    if "partner_id" not in fields:
        raise SalesInvoiceError("Kunde (partner_id) ist Pflicht.")
    invoice = SalesInvoice(
        tenant_id=company.tenant_id,
        company_id=company.id,
        status=SALES_INVOICE_DRAFT,
        invoice_type=SALES_INVOICE_TYPE_INVOICE,
        currency_code=invoice_currency(company),
        tax_treatment=(
            TAX_TREATMENT_SMALL_BUSINESS if company.is_small_business else TAX_TREATMENT_STANDARD
        ),
        einvoice_syntax=DEFAULT_SYNTAX,
        net_total=ZERO,
        tax_total=ZERO,
        gross_total=ZERO,
        created_by=changed_by,
    )
    _apply_fields(session, company, invoice, fields)
    session.add(invoice)
    session.flush()
    _audit(session, invoice, "created", changed_by, serialize_sales_invoice(invoice))
    session.commit()
    session.refresh(invoice)
    return invoice


def _require_draft(invoice: SalesInvoice) -> None:
    if invoice.status != SALES_INVOICE_DRAFT:
        label = invoice.invoice_number or invoice.id
        raise SalesInvoiceStateError(
            f"Rechnung {label} ist ausgestellt und unveränderlich; korrigieren Sie sie per Storno."
        )


def update_sales_invoice(
    *, session: Session, invoice: SalesInvoice, changed_by: str, **fields: Any
) -> SalesInvoice:
    """Ändert einen Entwurf; ``lines`` ersetzt alle Positionen."""
    _require_draft(invoice)
    if not fields:
        raise SalesInvoiceError("Mindestens ein änderbares Feld ist erforderlich.")
    company = session.get(Company, invoice.company_id)
    before = serialize_sales_invoice(invoice)
    _apply_fields(session, company, invoice, fields)
    session.flush()
    session.refresh(invoice)
    after = serialize_sales_invoice(invoice)
    if before != after:
        _audit(session, invoice, "updated", changed_by, {"before": before, "after": after})
    session.commit()
    session.refresh(invoice)
    return invoice


def delete_sales_invoice(*, session: Session, invoice: SalesInvoice, changed_by: str) -> None:
    """Löscht einen Entwurf. Er hat keine Nummer, der Nummernkreis bleibt lückenlos."""
    _require_draft(invoice)
    _audit(session, invoice, "deleted", changed_by, serialize_sales_invoice(invoice))
    session.delete(invoice)
    session.commit()


# ---------------------------------------------------------------------------
# Prüfungen vor dem Ausstellen
# ---------------------------------------------------------------------------


def _single_receivable_account(session: Session, company_id: int) -> list[Account]:
    return list(
        session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.subledger == SUBLEDGER_DEBTOR,
                Account.is_active.is_(True),
            )
            .order_by(Account.code)
        ).scalars()
    )


def _resolve_receivable_account(
    session: Session, invoice: SalesInvoice, problems: list[str]
) -> Account | None:
    if invoice.receivable_account_id is not None:
        account = session.get(Account, invoice.receivable_account_id)
        if account is not None and not account.is_active:
            problems.append(f"Das Debitoren-Sammelkonto {account.code} ist inaktiv.")
        return account
    accounts = _single_receivable_account(session, invoice.company_id)
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        problems.append(
            "Es gibt kein Debitoren-Sammelkonto. Bitte in der Kontenpflege ein Konto (z. B. 1400) "
            "als Debitoren-Sammelkonto kennzeichnen."
        )
    else:
        codes = ", ".join(account.code for account in accounts)
        problems.append(f"Mehrere Debitoren-Sammelkonten ({codes}): bitte eines auswählen.")
    return None


def _partner_problems(partner: BusinessPartner, problems: list[str]) -> None:
    if not partner.is_active:
        problems.append(f"Der Kunde {partner.name} ist inaktiv.")
    if partner.debtor_number is None:
        problems.append(f"{partner.name} ist kein Kunde (keine Debitorennummer).")
    missing = [
        label
        for label, value in (
            ("Straße", partner.street),
            ("PLZ", partner.postal_code),
            ("Ort", partner.city),
        )
        if not value
    ]
    if missing:
        problems.append(f"Anschrift des Kunden unvollständig: {', '.join(missing)} fehlt.")


def _seller_problems(seller: SellerData, problems: list[str]) -> None:
    missing = [
        label
        for label, value in (
            ("Straße", seller.street),
            ("PLZ", seller.postal_code),
            ("Ort", seller.city),
        )
        if not value
    ]
    if missing:
        problems.append(
            f"Anschrift der eigenen Firma unvollständig: {', '.join(missing)} fehlt "
            "(Verwaltung → Firmendaten)."
        )
    if not seller.vat_id and not seller.tax_number:
        problems.append(
            "Steuernummer oder USt-IdNr der eigenen Firma fehlt (Verwaltung → Firmendaten)."
        )


def _service_problems(invoice: SalesInvoice, problems: list[str]) -> None:
    has_date = invoice.service_date is not None
    period_from, period_to = invoice.service_period_from, invoice.service_period_to
    has_period = period_from is not None or period_to is not None
    if has_date and has_period:
        problems.append(
            "Bitte entweder Leistungsdatum oder Leistungszeitraum angeben, nicht beides."
        )
    elif not has_date and not has_period:
        problems.append("Leistungsdatum oder Leistungszeitraum fehlt.")
    elif has_period and (period_from is None or period_to is None):
        problems.append("Der Leistungszeitraum braucht Beginn und Ende.")
    elif has_period and period_from > period_to:
        problems.append("Der Beginn des Leistungszeitraums liegt nach dem Ende.")


def _line_problems(session: Session, invoice: SalesInvoice, problems: list[str]) -> None:
    if not invoice.lines:
        problems.append("Die Rechnung braucht mindestens eine Position.")
    standard = invoice.tax_treatment == TAX_TREATMENT_STANDARD
    for line in invoice.lines:
        label = f"Position {line.line_number}"
        if line.revenue_account_id is None:
            problems.append(f"{label}: Erlöskonto fehlt.")
        else:
            account = session.get(Account, line.revenue_account_id)
            if not account.is_active:
                problems.append(f"{label}: Das Erlöskonto {account.code} ist inaktiv.")
        tax_code = session.get(TaxCode, line.tax_code_id) if line.tax_code_id else None
        if tax_code is None:
            if standard:
                problems.append(f"{label}: Steuercode fehlt.")
            continue
        if not tax_code.is_active:
            problems.append(f"{label}: Steuercode {tax_code.code} ist inaktiv.")
        if standard and tax_code.rate == 0:
            problems.append(
                f"{label}: Steuercode {tax_code.code} hat 0 %. Für steuerfreie Umsätze bitte die "
                "passende Steuerbehandlung wählen."
            )
        elif standard and tax_code.vat_account_id is None:
            problems.append(f"{label}: Steuercode {tax_code.code} hat kein Steuerkonto.")
        elif not standard and tax_code.rate > 0:
            problems.append(
                f"{label}: Steuercode {tax_code.code} weist {tax_code.rate} % aus, die "
                f"Steuerbehandlung erlaubt keinen Steuerausweis."
            )


def _treatment_problems(
    invoice: SalesInvoice,
    company: Company,
    partner: BusinessPartner,
    seller: SellerData,
    problems: list[str],
) -> None:
    treatment = invoice.tax_treatment
    if company.is_small_business and treatment != TAX_TREATMENT_SMALL_BUSINESS:
        problems.append(
            "Die Firma ist als Kleinunternehmer (§ 19 UStG) gekennzeichnet: Bitte die "
            "Steuerbehandlung Kleinunternehmer wählen."
        )
    if treatment == TAX_TREATMENT_SMALL_BUSINESS and not company.is_small_business:
        problems.append(
            "Die Steuerbehandlung Kleinunternehmer setzt das Kennzeichen in den Firmendaten voraus."
        )
    country = (partner.country_code or "").upper()
    if treatment == TAX_TREATMENT_REVERSE_CHARGE and not partner.vat_id:
        problems.append("Reverse Charge verlangt die USt-IdNr des Kunden.")
    if treatment == TAX_TREATMENT_INTRA_COMMUNITY:
        if not seller.vat_id:
            problems.append("Innergemeinschaftliche Lieferung verlangt die eigene USt-IdNr.")
        if not partner.vat_id:
            problems.append("Innergemeinschaftliche Lieferung verlangt die USt-IdNr des Kunden.")
        if country == seller.country_code or country not in EU_COUNTRY_CODES:
            problems.append(
                "Innergemeinschaftliche Lieferung verlangt einen Kunden in einem anderen "
                f"EU-Mitgliedstaat (Land des Kunden: {country or 'fehlt'})."
            )
    if treatment == TAX_TREATMENT_EXPORT and (not country or country in EU_COUNTRY_CODES):
        problems.append(
            "Ausfuhrlieferung verlangt einen Kunden außerhalb der EU "
            f"(Land des Kunden: {country or 'fehlt'})."
        )


def _invoice_rate(session: Session, invoice: SalesInvoice, company: Company) -> Decimal | None:
    """Kurs der Rechnung: erfasst, sonst der Kurs am Leistungsdatum; None in Firmenwährung.

    § 16 Abs. 6 UStG knüpft an den Monat der Leistung an. Liegt sie noch in der Zukunft, ist ihr
    Kurs unbekannt; dann gilt das Rechnungsdatum.
    """
    if invoice.currency_code == company.currency_code:
        return None
    if invoice.exchange_rate is not None:
        return invoice.exchange_rate
    performed_on = invoice.service_date or invoice.service_period_to or invoice.issue_date
    return rate_for(
        session=session,
        tenant_id=company.tenant_id,
        currency_code=invoice.currency_code,
        on=min(performed_on, invoice.issue_date),
    )


def _currency_problems(
    session: Session, invoice: SalesInvoice, company: Company, problems: list[str]
) -> None:
    if invoice.currency_code == company.currency_code or invoice.issue_date is None:
        return
    try:
        _invoice_rate(session, invoice, company)
    except ExchangeRateError as exc:
        problems.append(str(exc))


def issue_problems(*, session: Session, invoice: SalesInvoice) -> list[str]:
    """Alle Gründe, warum der Entwurf noch nicht ausgestellt werden kann."""
    company = session.get(Company, invoice.company_id)
    partner = session.get(BusinessPartner, invoice.partner_id)
    seller = resolve_seller(company)
    problems: list[str] = []
    _seller_problems(seller, problems)
    _partner_problems(partner, problems)
    _service_problems(invoice, problems)
    _line_problems(session, invoice, problems)
    _treatment_problems(invoice, company, partner, seller, problems)
    _currency_problems(session, invoice, company, problems)
    _resolve_receivable_account(session, invoice, problems)
    if invoice.lines and compute_invoice_totals(invoice.lines).gross <= 0:
        problems.append("Der Gesamtbetrag muss größer als 0 sein.")
    return problems


def einvoice_hints(*, session: Session, invoice: SalesInvoice) -> list[str]:
    """Hinweise, die das Ausstellen nicht verhindern: XRechnung-Felder und UStVA-Konten."""
    company = session.get(Company, invoice.company_id)
    partner = session.get(BusinessPartner, invoice.partner_id)
    seller = resolve_seller(company)
    hints: list[str] = []
    missing = [
        label
        for label, value in (
            ("Telefon", seller.phone),
            ("E-Mail", seller.email),
            ("IBAN", seller.iban),
        )
        if not value
    ]
    if missing:
        hints.append(
            f"Für eine vollständige XRechnung fehlen in den Firmendaten: {', '.join(missing)}."
        )
    if not partner.email:
        hints.append("Für eine vollständige XRechnung fehlt die E-Mail-Adresse des Kunden (BT-49).")
    if not invoice.buyer_reference:
        hints.append(
            "Ohne Leitweg-ID oder Käuferreferenz trägt die XRechnung die Debitorennummer als "
            "Referenz (BT-10). Öffentliche Auftraggeber verlangen die Leitweg-ID."
        )
    rule = TAX_TREATMENT_RULES[invoice.tax_treatment]
    if rule.kennzahlen:
        chart = detect_company_chart(session=session, company_id=invoice.company_id)
        for line in invoice.lines:
            if line.revenue_account_id is None:
                continue
            account = session.get(Account, line.revenue_account_id)
            if revenue_kennzahl(chart, account.code) not in rule.kennzahlen:
                hints.append(
                    f"Position {line.line_number}: Das Konto {account.code} passt in der UStVA "
                    f"nicht zur Steuerbehandlung (üblich: {rule.account_hint})."
                )
    return hints


# ---------------------------------------------------------------------------
# Nummernkreis
# ---------------------------------------------------------------------------


def next_invoice_number(*, session: Session, company: Company, invoice_date: date) -> str:
    """Zieht die nächste Nummer in der laufenden Transaktion.

    Das ``UPDATE`` hält die Zeilensperre bis zum Commit (PostgreSQL) bzw. die Schreibsperre
    (SQLite); parallele Vorgänge warten also, und ein Rollback nimmt die Nummer zurück. Legt eine
    parallele Transaktion die Jahreszeile gleichzeitig an, gewinnt der UNIQUE-Constraint.
    """
    year = invoice_date.year
    conditions = (
        SalesInvoiceSequence.company_id == company.id,
        SalesInvoiceSequence.year == year,
    )
    increment = (
        update(SalesInvoiceSequence)
        .where(*conditions)
        .values(last_number=SalesInvoiceSequence.last_number + 1)
        .execution_options(synchronize_session=False)
    )
    if session.execute(increment).rowcount == 0:
        try:
            with session.begin_nested():
                session.add(
                    SalesInvoiceSequence(
                        tenant_id=company.tenant_id,
                        company_id=company.id,
                        year=year,
                        last_number=0,
                    )
                )
        except IntegrityError:
            pass
        session.execute(increment)
    number = session.execute(
        select(SalesInvoiceSequence.last_number).where(*conditions)
    ).scalar_one()
    return f"{company.invoice_prefix}-{year}-{number:04d}"


# ---------------------------------------------------------------------------
# Belege und Ausgabe
# ---------------------------------------------------------------------------


def _store_document(
    *,
    session: Session,
    company: Company,
    upload_dir: Path,
    written: list[Path],
    journal_entry_id: int,
    file_name: str,
    mime_type: str,
    content: bytes,
    document_date: date,
) -> Document:
    target_dir = Path(upload_dir) / str(company.tenant_id) / str(company.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid4().hex}_{file_name}"
    target_path.write_bytes(content)
    written.append(target_path)
    metadata = document_file_metadata(content)
    category = find_document_category_by_name(
        session=session, company_id=company.id, name="Rechnung"
    )
    document = Document(
        tenant_id=company.tenant_id,
        company_id=company.id,
        journal_entry_id=journal_entry_id,
        category_id=category.id if category else None,
        file_name=file_name,
        storage_key=str(target_path),
        mime_type=mime_type,
        file_sha256=metadata.file_sha256,
        file_size_bytes=metadata.file_size_bytes,
        document_date=document_date,
        capture_source=CAPTURE_SOURCE_SALES_INVOICE,
    )
    session.add(document)
    session.flush()
    return document


def _date_text(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _buyer_party(invoice: SalesInvoice, partner: BusinessPartner) -> Party:
    return Party(
        name=invoice.recipient_name or partner.name,
        street=invoice.recipient_street or "",
        postal_code=invoice.recipient_postal_code or "",
        city=invoice.recipient_city or "",
        country_code=invoice.recipient_country_code or "DE",
        vat_id=invoice.recipient_vat_id or "",
        contact_name=partner.contact_person or "",
        phone=partner.phone or "",
        email=partner.email or "",
    )


def _payment_terms(invoice: SalesInvoice, original: SalesInvoice | None) -> str:
    if original is not None:
        return f"Storno der Rechnung {original.invoice_number}; es ist keine Zahlung fällig."
    if invoice.due_date is not None:
        return f"Zahlbar bis {_date_text(invoice.due_date)} ohne Abzug."
    return "Zahlbar sofort ohne Abzug."


def _outgoing_invoice(
    *,
    invoice: SalesInvoice,
    company: Company,
    partner: BusinessPartner,
    original: SalesInvoice | None = None,
) -> OutgoingInvoice:
    """E-Rechnung zur Rechnung. Eine Stornorechnung (381) trägt positive Beträge."""
    rule = TAX_TREATMENT_RULES[invoice.tax_treatment]
    sign = Decimal("-1") if original is not None else Decimal("1")
    notes = [invoice.header_text or "", _tax_note(invoice, company), invoice.footer_text or ""]
    return OutgoingInvoice(
        invoice_number=invoice.invoice_number,
        issue_date=invoice.issue_date,
        seller=seller_party(company),
        buyer=_buyer_party(invoice, partner),
        lines=[
            InvoiceLine(
                name=line.description,
                quantity=sign * line.quantity,
                unit_price=line.unit_price,
                tax_rate=line.tax_rate,
                unit_code=line.unit_code,
            )
            for line in invoice.lines
        ],
        currency_code=invoice.currency_code,
        tax_currency_code=company.currency_code if invoice.exchange_rate else "",
        tax_total_in_tax_currency=(
            abs(_ledger_tax_total(invoice)) if invoice.exchange_rate else None
        ),
        buyer_reference=invoice.buyer_reference or "",
        type_code=INVOICE_TYPE_CREDIT_NOTE if original is not None else INVOICE_TYPE_COMMERCIAL,
        due_date=invoice.due_date,
        service_date=invoice.service_date,
        service_period_from=invoice.service_period_from,
        service_period_to=invoice.service_period_to,
        tax_category=rule.category,
        exemption_reason=rule.note,
        exemption_reason_code=rule.reason_code,
        payment_iban=company.iban or "",
        payment_bic=company.bic or "",
        payment_account_name=company.name,
        payment_reference=invoice.invoice_number,
        payment_terms=_payment_terms(invoice, original),
        notes=[note for note in notes if note],
        preceding_invoice_number=original.invoice_number if original is not None else "",
        preceding_invoice_date=original.issue_date if original is not None else None,
    )


def _recipient_lines(invoice: SalesInvoice, seller: SellerData) -> list[str]:
    lines = [invoice.recipient_name or ""]
    if invoice.recipient_street:
        lines.append(invoice.recipient_street)
    postal = " ".join(
        part for part in (invoice.recipient_postal_code, invoice.recipient_city) if part
    )
    if postal:
        lines.append(postal)
    country = invoice.recipient_country_code
    if country and country != seller.country_code:
        lines.append(country)
    return lines


def _service_text(invoice: SalesInvoice) -> tuple[str, str]:
    if invoice.service_date is not None:
        return "Leistungsdatum", _date_text(invoice.service_date)
    return (
        "Leistungszeitraum",
        f"{_date_text(invoice.service_period_from)} bis {_date_text(invoice.service_period_to)}",
    )


def _payment_lines(invoice: SalesInvoice, seller: SellerData, *, is_storno: bool) -> list[str]:
    if is_storno:
        return ["Diese Stornorechnung hebt die genannte Rechnung vollständig auf."]
    terms = (
        f"Bitte überweisen Sie den Gesamtbetrag bis zum {_date_text(invoice.due_date)}"
        if invoice.due_date is not None
        else "Der Gesamtbetrag ist sofort fällig; bitte überweisen Sie ihn"
    )
    if seller.iban:
        bank = " · ".join(
            part
            for part in (
                seller.bank_name,
                f"IBAN {seller.iban}",
                f"BIC {seller.bic}" if seller.bic else "",
            )
            if part
        )
        return [f"{terms} auf folgendes Konto:", bank,
                f"Verwendungszweck: {invoice.invoice_number}"]
    return [f"{terms} unter Angabe der Rechnungsnummer {invoice.invoice_number}."]


def _ledger_tax_total(invoice: SalesInvoice) -> Decimal:
    """Umsatzsteuer in Firmenwährung, wie sie gebucht wird."""
    return sum(
        (ledger_amount(invoice, tax) for tax in _tax_by_code(invoice.lines).values()), ZERO
    )


def _tax_note(invoice: SalesInvoice, company: Company) -> str:
    """Pflichthinweise: Steuerbehandlung, Umrechnung und ab 2028 die Istversteuerung."""
    parts = [TAX_TREATMENT_RULES[invoice.tax_treatment].note]
    if invoice.exchange_rate is not None:
        rate_text = format_rate_text(company, invoice.currency_code, invoice.exchange_rate)
        parts.append(f"Umrechnungskurs: {rate_text}.")
        tax = abs(_ledger_tax_total(invoice))
        if tax:
            # § 16 Abs. 6 UStG: Die Umsatzsteuer wird in Euro geschuldet.
            parts.append(f"Umsatzsteuer: {format_amount(tax)} {company.currency_code}.")
    if invoice.issue_date >= CASH_BASIS_NOTE_FROM and company_on_cash_basis(
        company, invoice.issue_date
    ):
        parts.append(CASH_BASIS_NOTE)
    return " ".join(part for part in parts if part)


def _pdf_invoice(
    *,
    invoice: SalesInvoice,
    company: Company,
    seller: SellerData,
    partner: BusinessPartner,
    original: SalesInvoice | None,
) -> PdfInvoice:
    totals = compute_invoice_totals(invoice.lines)
    service_label, service_text = _service_text(invoice)
    return PdfInvoice(
        title="Stornorechnung" if original is not None else "Rechnung",
        invoice_number=invoice.invoice_number,
        issue_date=invoice.issue_date,
        seller=seller,
        recipient_lines=_recipient_lines(invoice, seller),
        lines=[
            PdfInvoiceLine(
                description=line.description,
                quantity=line.quantity,
                unit_label=UNIT_LABELS.get(line.unit_code, line.unit_code),
                unit_price=line.unit_price,
                tax_rate=line.tax_rate,
                net_amount=line.net_amount,
            )
            for line in invoice.lines
        ],
        tax_groups=totals.groups,
        net_total=totals.net,
        tax_total=totals.tax,
        gross_total=totals.gross,
        currency_code=invoice.currency_code,
        service_label=service_label,
        service_text=service_text,
        customer_number=partner.debtor_number or "",
        buyer_reference=invoice.buyer_reference or "",
        recipient_vat_id=invoice.recipient_vat_id or "",
        reference_text=(
            f"Storno der Rechnung {original.invoice_number} vom "
            f"{_date_text(original.issue_date)}"
            if original is not None
            else ""
        ),
        header_text=invoice.header_text or "",
        tax_note=_tax_note(invoice, company),
        payment_lines=_payment_lines(invoice, seller, is_storno=original is not None),
        footer_text=invoice.footer_text or "",
    )


def _store_output_documents(
    *,
    session: Session,
    invoice: SalesInvoice,
    company: Company,
    partner: BusinessPartner,
    journal_entry_id: int,
    upload_dir: Path,
    written: list[Path],
    original: SalesInvoice | None = None,
) -> tuple[Document, Document]:
    """Erzeugt XML und PDF (mit eingebettetem XML) und legt beide als Belege ab."""
    seller = resolve_seller(company)
    xml_text = build_einvoice(
        _outgoing_invoice(invoice=invoice, company=company, partner=partner, original=original),
        syntax=invoice.einvoice_syntax,
    )
    xml_bytes = xml_text.encode("utf-8")
    prefix = "Stornorechnung" if original is not None else "Rechnung"
    base_name = secure_filename(f"{prefix}_{invoice.invoice_number}") or prefix
    pdf_bytes = embed_xml_attachment(
        build_invoice_pdf(
            _pdf_invoice(
                invoice=invoice,
                company=company,
                seller=seller,
                partner=partner,
                original=original,
            )
        ),
        file_name=f"{base_name}.xml",
        xml_bytes=xml_bytes,
    )
    common = {
        "session": session,
        "company": company,
        "upload_dir": upload_dir,
        "written": written,
        "journal_entry_id": journal_entry_id,
        "document_date": invoice.issue_date,
    }
    xml_document = _store_document(
        **common, file_name=f"{base_name}.xml", mime_type="application/xml", content=xml_bytes
    )
    pdf_document = _store_document(
        **common, file_name=f"{base_name}.pdf", mime_type="application/pdf", content=pdf_bytes
    )
    return pdf_document, xml_document


def _remove_files(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Ausstellen
# ---------------------------------------------------------------------------


def _tax_by_code(lines: Sequence[SalesInvoiceLine]) -> dict[int, Decimal]:
    """Steuer je Steuercode so, dass die Summe je Satz der Rechnung entspricht.

    Teilen sich mehrere Steuercodes einen Satz, trägt der mit der größten Grundlage die
    Rundungsdifferenz.
    """
    net_by_code: dict[int, Decimal] = {}
    rate_by_code: dict[int, Decimal] = {}
    for line in lines:
        if line.tax_code_id is None or line.tax_rate == 0:
            continue
        net_by_code[line.tax_code_id] = net_by_code.get(line.tax_code_id, ZERO) + line.net_amount
        rate_by_code[line.tax_code_id] = line.tax_rate
    result: dict[int, Decimal] = {}
    for group in compute_tax_groups((line.tax_rate, line.net_amount) for line in lines):
        codes = [code for code, rate in rate_by_code.items() if rate == group.rate]
        for code in codes:
            result[code] = (net_by_code[code] * group.rate / Decimal("100")).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
        if codes:
            largest = max(codes, key=lambda code: abs(net_by_code[code]))
            result[largest] += group.tax_amount - sum(result[code] for code in codes)
    return result


def _signed_line(account_id: int, amount: Decimal, **kwargs: Any) -> JournalLineInput:
    """Erlös- oder Steuerzeile im Haben; ein negativer Saldo (Nachlass) steht im Soll."""
    return JournalLineInput(
        account_id=account_id,
        debit_amount=-amount if amount < 0 else ZERO,
        credit_amount=amount if amount > 0 else ZERO,
        **kwargs,
    )


def _booking_lines(
    session: Session, invoice: SalesInvoice, receivable_account_id: int
) -> list[JournalLineInput]:
    """Soll Sammelkonto brutto, Haben Erlöse netto je Konto und Steuercode, Haben Steuer."""
    net_by_target: dict[tuple[int, int | None], Decimal] = {}
    for line in invoice.lines:
        key = (line.revenue_account_id, line.tax_code_id)
        net_by_target[key] = net_by_target.get(key, ZERO) + line.net_amount
    label = f"Rechnung {invoice.invoice_number}"
    gross = compute_invoice_totals(invoice.lines).gross
    foreign_fields: dict[str, Any] = {}
    if invoice.exchange_rate is not None:
        foreign_fields = {
            "foreign_currency_code": invoice.currency_code,
            "foreign_amount": gross,
            "exchange_rate": invoice.exchange_rate,
        }
    receivable = JournalLineInput(
        account_id=receivable_account_id,
        debit_amount=ledger_amount(invoice, gross),
        description=label,
        **foreign_fields,
    )
    revenue_lines = []
    for (account_id, tax_code_id), net in net_by_target.items():
        if net == 0:
            continue
        revenue_lines.append(
            _signed_line(
                account_id, ledger_amount(invoice, net), description=label, tax_code_id=tax_code_id
            )
        )
    tax_lines = []
    for tax_code_id, tax in _tax_by_code(invoice.lines).items():
        if tax == 0:
            continue
        tax_code = session.get(TaxCode, tax_code_id)
        tax_amount = ledger_amount(invoice, tax)
        # Der Fremdbetrag ist die Grundlage der Steuer zum Monatskurs bei Istversteuerung (ADR-010):
        # Aus dem gerundeten Eurobetrag ergäbe er sich bei hohen Kursen nicht auf den Cent genau.
        tax_foreign_fields = (
            {**foreign_fields, "foreign_amount": abs(tax)}
            if foreign_fields and tax_amount != 0
            else {}
        )
        tax_lines.append(
            _signed_line(
                tax_code.vat_account_id,
                tax_amount,
                description=f"{tax_code.code} {tax_code.rate}% {label}",
                tax_code_id=tax_code_id,
                **tax_foreign_fields,
            )
        )
    # Einzeln umgerechnet kann die Summe um Cents vom Brutto abweichen. Die Rundung liegt auf dem
    # größten Erlös, denn Forderung und Umsatzsteuer müssen genau dem Kurs entsprechen.
    credits = [*revenue_lines, *tax_lines]
    rounding = receivable.debit_amount - sum(
        (line.credit_amount - line.debit_amount for line in credits), ZERO
    )
    if rounding != 0 and revenue_lines:
        largest = max(revenue_lines, key=lambda line: line.credit_amount + line.debit_amount)
        if largest.credit_amount > 0:
            largest.credit_amount += rounding
        else:
            largest.debit_amount -= rounding
    return [receivable, *credits]


def _load_for_update(session: Session, invoice_id: int) -> SalesInvoice:
    invoice = session.execute(
        select(SalesInvoice)
        .where(SalesInvoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if invoice is None:
        raise SalesInvoiceError("Rechnung nicht gefunden.")
    return invoice


def _snapshot_recipient(invoice: SalesInvoice, partner: BusinessPartner) -> None:
    invoice.recipient_name = partner.name
    invoice.recipient_street = partner.street
    invoice.recipient_postal_code = partner.postal_code
    invoice.recipient_city = partner.city
    invoice.recipient_country_code = partner.country_code
    invoice.recipient_vat_id = partner.vat_id


def issue_sales_invoice(
    *,
    session: Session,
    invoice_id: int,
    changed_by: str,
    upload_dir: Path,
    issue_date: date | None = None,
    receivable_account_id: int | None = None,
) -> SalesInvoice:
    """Stellt den Entwurf atomar aus; siehe Modulkopf. Fehlende Angaben: ``problems``."""
    written: list[Path] = []
    try:
        invoice = _issue(
            session=session,
            invoice_id=invoice_id,
            changed_by=changed_by,
            upload_dir=upload_dir,
            issue_date=issue_date,
            receivable_account_id=receivable_account_id,
            written=written,
        )
        session.commit()
    except (
        JournalEntryCreationError,
        JournalEntryValidationError,
        OpenItemError,
        EInvoiceExportError,
    ) as exc:
        session.rollback()
        _remove_files(written)
        raise SalesInvoiceError(f"Ausstellen fehlgeschlagen: {exc}") from exc
    except Exception:
        session.rollback()
        _remove_files(written)
        raise
    session.refresh(invoice)
    return invoice


def _issue(
    *,
    session: Session,
    invoice_id: int,
    changed_by: str,
    upload_dir: Path,
    issue_date: date | None,
    receivable_account_id: int | None,
    written: list[Path],
) -> SalesInvoice:
    invoice = _load_for_update(session, invoice_id)
    _require_draft(invoice)
    company = session.get(Company, invoice.company_id)
    partner = session.get(BusinessPartner, invoice.partner_id)
    if receivable_account_id is not None:
        account = _load_receivable_account(session, company.id, receivable_account_id)
        invoice.receivable_account_id = account.id
    invoice.issue_date = issue_date or invoice.issue_date or date.today()

    # Der Steuersatz kommt aus dem aktuellen Steuercode, nicht aus dem Stand des Entwurfs.
    for line in invoice.lines:
        tax_code = session.get(TaxCode, line.tax_code_id) if line.tax_code_id else None
        line.tax_rate = tax_code.rate if tax_code else ZERO

    problems = issue_problems(session=session, invoice=invoice)
    if problems:
        session.rollback()
        raise SalesInvoiceError(
            "Die Rechnung kann noch nicht ausgestellt werden: " + " ".join(problems),
            problems=problems,
        )

    receivable = _resolve_receivable_account(session, invoice, [])
    invoice.receivable_account_id = receivable.id
    _snapshot_recipient(invoice, partner)
    try:
        invoice.exchange_rate = _invoice_rate(session, invoice, company)
    except ExchangeRateError as exc:
        raise SalesInvoiceError(str(exc)) from exc
    if not invoice.buyer_reference:
        # BT-10 ist in der XRechnung Pflicht; ohne Angabe gilt die Kundennummer.
        invoice.buyer_reference = partner.debtor_number
    if invoice.due_date is None and partner.payment_term_days is not None:
        invoice.due_date = invoice.issue_date + timedelta(days=partner.payment_term_days)
    _set_totals(invoice)

    invoice.invoice_number = next_invoice_number(
        session=session, company=company, invoice_date=invoice.issue_date
    )
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=invoice.issue_date,
            description=f"Ausgangsrechnung {invoice.invoice_number} {partner.name}",
            status="posted",
            changed_by=changed_by,
            lines=_booking_lines(session, invoice, receivable.id),
            source=SOURCE_SALES_INVOICE,
            # Steuerzeilen sind explizit: Die Steuer je Satz muss der Rechnung entsprechen.
            expand_tax_lines=False,
            service_date=invoice.service_date or invoice.service_period_to,
            partner_id=partner.id,
        ),
        commit=False,
    )
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=receivable.id,
            item_type="receivable",
            reference=invoice.invoice_number,
            entry_date=invoice.issue_date,
            amount=ledger_amount(invoice, invoice.gross_total),
            changed_by=changed_by,
            journal_entry_id=entry.id,
            counterparty=partner.name,
            due_date=invoice.due_date,
            partner_id=partner.id,
            foreign_currency_code=invoice.currency_code if invoice.exchange_rate else None,
            foreign_amount=invoice.gross_total if invoice.exchange_rate else None,
            exchange_rate=invoice.exchange_rate,
        ),
        commit=False,
    )
    pdf_document, xml_document = _store_output_documents(
        session=session,
        invoice=invoice,
        company=company,
        partner=partner,
        journal_entry_id=entry.id,
        upload_dir=upload_dir,
        written=written,
    )
    invoice.journal_entry_id = entry.id
    invoice.open_item_id = item.id
    invoice.pdf_document_id = pdf_document.id
    invoice.xml_document_id = xml_document.id
    invoice.issued_by = changed_by
    invoice.issued_at = datetime.now(timezone.utc)
    # Als Letztes: Danach schützt der Trigger die Zeile.
    invoice.status = SALES_INVOICE_ISSUED
    session.flush()
    _audit(
        session,
        invoice,
        "issued",
        changed_by,
        {
            "invoice_number": invoice.invoice_number,
            "partner_id": invoice.partner_id,
            "issue_date": invoice.issue_date.isoformat(),
            "tax_treatment": invoice.tax_treatment,
            "net_total": str(invoice.net_total),
            "tax_total": str(invoice.tax_total),
            "gross_total": str(invoice.gross_total),
            "journal_entry_id": entry.id,
            "posting_number": entry.posting_number,
            "open_item_id": item.id,
            "pdf_document_id": pdf_document.id,
            "pdf_sha256": pdf_document.file_sha256,
            "xml_document_id": xml_document.id,
            "xml_sha256": xml_document.file_sha256,
        },
    )
    return invoice


# ---------------------------------------------------------------------------
# Storno
# ---------------------------------------------------------------------------


def cancel_sales_invoice(
    *,
    session: Session,
    invoice_id: int,
    changed_by: str,
    upload_dir: Path,
    cancellation_date: date | None = None,
    reason: str | None = None,
) -> SalesInvoice:
    """Storniert eine unbezahlte Rechnung und liefert die Stornorechnung; alles oder nichts."""
    written: list[Path] = []
    try:
        cancellation = _cancel(
            session=session,
            invoice_id=invoice_id,
            changed_by=changed_by,
            upload_dir=upload_dir,
            cancellation_date=cancellation_date or date.today(),
            reason=reason,
            written=written,
        )
        session.commit()
    except (
        JournalEntryCreationError,
        JournalEntryValidationError,
        OpenItemError,
        EInvoiceExportError,
    ) as exc:
        session.rollback()
        _remove_files(written)
        raise SalesInvoiceError(f"Storno fehlgeschlagen: {exc}") from exc
    except Exception:
        session.rollback()
        _remove_files(written)
        raise
    session.refresh(cancellation)
    return cancellation


def _cancel(
    *,
    session: Session,
    invoice_id: int,
    changed_by: str,
    upload_dir: Path,
    cancellation_date: date,
    reason: str | None,
    written: list[Path],
) -> SalesInvoice:
    original = _load_for_update(session, invoice_id)
    if original.invoice_type == SALES_INVOICE_TYPE_CANCELLATION:
        raise SalesInvoiceStateError("Eine Stornorechnung kann nicht storniert werden.")
    if original.status == SALES_INVOICE_DRAFT:
        raise SalesInvoiceStateError("Ein Entwurf wird nicht storniert, sondern gelöscht.")
    if original.status == SALES_INVOICE_CANCELLED:
        raise SalesInvoiceStateError(f"Rechnung {original.invoice_number} ist bereits storniert.")
    item = original.open_item
    if item is None or item.status != "open" or item.open_amount != item.original_amount:
        paid = (item.original_amount - item.open_amount) if item is not None else ZERO
        # Der Posten führt Firmenwährung, auch bei einer Rechnung in Fremdwährung.
        paid_currency = item.currency_code if item is not None else original.currency_code
        raise SalesInvoiceStateError(
            f"Rechnung {original.invoice_number} ist bereits (teil-)bezahlt "
            f"({format_amount(paid)} {paid_currency}). Eine Stornierung ist nur bei "
            "unbezahlten Rechnungen möglich; die Zahlung muss zuerst zurückgenommen werden."
        )
    if cancellation_date < original.issue_date:
        raise SalesInvoiceError(
            f"Das Stornodatum darf nicht vor dem Rechnungsdatum "
            f"{_date_text(original.issue_date)} liegen."
        )
    company = session.get(Company, original.company_id)
    partner = session.get(BusinessPartner, original.partner_id)
    reason_text = _text(reason, "reason", TEXT_LIMIT)

    cancellation = SalesInvoice(
        tenant_id=original.tenant_id,
        company_id=original.company_id,
        partner_id=original.partner_id,
        status=SALES_INVOICE_DRAFT,
        invoice_type=SALES_INVOICE_TYPE_CANCELLATION,
        cancels_invoice_id=original.id,
        issue_date=cancellation_date,
        service_date=original.service_date,
        service_period_from=original.service_period_from,
        service_period_to=original.service_period_to,
        currency_code=original.currency_code,
        exchange_rate=original.exchange_rate,
        recipient_name=original.recipient_name,
        recipient_street=original.recipient_street,
        recipient_postal_code=original.recipient_postal_code,
        recipient_city=original.recipient_city,
        recipient_country_code=original.recipient_country_code,
        recipient_vat_id=original.recipient_vat_id,
        buyer_reference=original.buyer_reference,
        tax_treatment=original.tax_treatment,
        einvoice_syntax=original.einvoice_syntax,
        header_text=reason_text,
        receivable_account_id=original.receivable_account_id,
        open_item_id=item.id,
        created_by=changed_by,
        lines=[
            SalesInvoiceLine(
                tenant_id=line.tenant_id,
                line_number=line.line_number,
                description=line.description,
                quantity=-line.quantity,
                unit_code=line.unit_code,
                unit_price=line.unit_price,
                tax_code_id=line.tax_code_id,
                revenue_account_id=line.revenue_account_id,
                tax_rate=line.tax_rate,
                net_amount=-line.net_amount,
            )
            for line in original.lines
        ],
    )
    _set_totals(cancellation)
    session.add(cancellation)
    session.flush()

    cancellation.invoice_number = next_invoice_number(
        session=session, company=company, invoice_date=cancellation_date
    )
    reversal: JournalEntry = reverse_journal_entry(
        session=session,
        journal_entry_id=original.journal_entry_id,
        reversal_date=cancellation_date,
        changed_by=changed_by,
        commit=False,
        from_sales_invoice=True,
    )
    close_open_item_by_cancellation(
        session=session, item=item, reversal_entry=reversal, changed_by=changed_by
    )
    pdf_document, xml_document = _store_output_documents(
        session=session,
        invoice=cancellation,
        company=company,
        partner=partner,
        journal_entry_id=reversal.id,
        upload_dir=upload_dir,
        written=written,
        original=original,
    )
    cancellation.journal_entry_id = reversal.id
    cancellation.pdf_document_id = pdf_document.id
    cancellation.xml_document_id = xml_document.id
    cancellation.issued_by = changed_by
    cancellation.issued_at = datetime.now(timezone.utc)
    cancellation.status = SALES_INVOICE_ISSUED
    original.status = SALES_INVOICE_CANCELLED
    session.flush()
    _audit(
        session,
        original,
        "cancelled",
        changed_by,
        {
            "invoice_number": original.invoice_number,
            "cancellation_invoice_id": cancellation.id,
            "cancellation_number": cancellation.invoice_number,
            "reversal_entry_id": reversal.id,
            "reversal_posting_number": reversal.posting_number,
            "reason": reason_text,
        },
    )
    _audit(
        session,
        cancellation,
        "issued",
        changed_by,
        {
            "invoice_number": cancellation.invoice_number,
            "cancels_invoice_number": original.invoice_number,
            "gross_total": str(cancellation.gross_total),
            "journal_entry_id": reversal.id,
            "pdf_document_id": pdf_document.id,
            "pdf_sha256": pdf_document.file_sha256,
            "xml_document_id": xml_document.id,
            "xml_sha256": xml_document.file_sha256,
        },
    )
    return cancellation
