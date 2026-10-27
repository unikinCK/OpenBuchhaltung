"""Import strukturierter elektronischer Rechnungen (XRechnung / ZUGFeRD).

Unterstützt beide in Deutschland relevanten XML-Syntaxen:
    * UN/CEFACT CII (Cross Industry Invoice) — u. a. ZUGFeRD und XRechnung-CII
    * OASIS UBL (Universal Business Language) — XRechnung-UBL

Der Parser arbeitet namespace-agnostisch (nur über die lokalen Elementnamen),
damit er mit den verschiedenen Namespace-Versionen der Formate umgeht. Er liefert
die für eine Verbuchung nötigen Kopfdaten (Rechnungsnummer, Datum, Lieferant,
Netto/Steuer/Brutto und die Steuersätze).

Rechnungen mit mehreren Steuersätzen (z. B. 7 % und 19 %) bucht
:func:`book_einvoice` je Satz mit eigener Aufwands- und Vorsteuerzeile.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from app.services.incoming_invoice import (
    IncomingInvoiceError,
    InvoiceTaxSplit,
    book_incoming_invoice,
    invoice_gross_amount,
)
from domain.formatting import format_amount
from domain.models import Company, JournalEntry, TaxCode


class EInvoiceParseError(ValueError):
    """Raised when an e-invoice XML cannot be parsed."""


@dataclass(slots=True)
class TaxBreakdown:
    rate: Decimal
    basis: Decimal
    tax_amount: Decimal


@dataclass(slots=True)
class ParsedInvoice:
    invoice_number: str
    issue_date: date
    seller_name: str
    currency_code: str
    net_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    syntax: str  # "CII" oder "UBL"
    tax_lines: list[TaxBreakdown] = field(default_factory=list)

    @property
    def primary_tax_rate(self) -> Decimal | None:
        if not self.tax_lines:
            return None
        # Steuersatz mit der größten Bemessungsgrundlage
        return max(self.tax_lines, key=lambda line: line.basis).rate


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, *local_names: str) -> ET.Element | None:
    """Steigt entlang der lokalen Elementnamen ab (erstes Kind je Ebene)."""
    current: ET.Element | None = element
    for name in local_names:
        if current is None:
            return None
        current = next(
            (child for child in current if _local(child.tag) == name), None
        )
    return current


def _find_all(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local(child.tag) == local_name]


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        raise EInvoiceParseError(f"Ungültiger Betrag: {value!r}") from None


def _parse_date(value: str, *, fmt_hint: str | None = None) -> date:
    value = value.strip()
    if fmt_hint == "102" or (value.isdigit() and len(value) == 8):
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    raise EInvoiceParseError(f"Ungültiges Rechnungsdatum: {value!r}")


def parse_einvoice(xml_bytes: bytes) -> ParsedInvoice:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise EInvoiceParseError(f"XML konnte nicht gelesen werden: {exc}") from exc

    root_name = _local(root.tag)
    if root_name in {"CrossIndustryInvoice", "CrossIndustryDocument"}:
        return _parse_cii(root)
    if root_name == "Invoice":
        return _parse_ubl(root)
    raise EInvoiceParseError(
        f"Unbekanntes Rechnungsformat (Wurzelelement {root_name!r}). "
        "Unterstützt werden CII (ZUGFeRD/XRechnung) und UBL (XRechnung)."
    )


def _parse_cii(root: ET.Element) -> ParsedInvoice:
    doc = _find(root, "ExchangedDocument")
    invoice_number = _text(_find(doc, "ID")) if doc is not None else ""

    issue_date = ""
    fmt_hint = None
    if doc is not None:
        dt = _find(doc, "IssueDateTime", "DateTimeString")
        if dt is not None:
            issue_date = _text(dt)
            fmt_hint = dt.get("format")

    seller = ""
    seller_party = None
    for candidate in _find_all(root, "SellerTradeParty"):
        seller_party = candidate
        break
    if seller_party is not None:
        seller = _text(_find(seller_party, "Name"))

    summation = next(
        iter(_find_all(root, "SpecifiedTradeSettlementHeaderMonetarySummation")), None
    )
    if summation is None:
        raise EInvoiceParseError("CII-Rechnung ohne Summenblock (MonetarySummation).")

    net_total = _decimal(_text(_find(summation, "LineTotalAmount")) or "0")
    tax_total = _decimal(_text(_find(summation, "TaxTotalAmount")) or "0")
    grand_total = _decimal(_text(_find(summation, "GrandTotalAmount")) or "0")

    currency = ""
    for tax_amount in _find_all(root, "TaxTotalAmount"):
        currency = tax_amount.get("currencyID") or currency
    if not currency:
        currency = _text(next(iter(_find_all(root, "InvoiceCurrencyCode")), None)) or "EUR"

    tax_lines: list[TaxBreakdown] = []
    for trade_tax in _find_all(root, "ApplicableTradeTax"):
        basis = _find(trade_tax, "BasisAmount")
        calc = _find(trade_tax, "CalculatedAmount")
        # ZUGFeRD 1 (CrossIndustryDocument) heißt das Feld ApplicablePercent.
        rate = _find(trade_tax, "RateApplicablePercent")
        if rate is None:
            rate = _find(trade_tax, "ApplicablePercent")
        if basis is None and calc is None:
            continue
        tax_lines.append(
            TaxBreakdown(
                rate=_decimal(_text(rate) or "0"),
                basis=_decimal(_text(basis) or "0"),
                tax_amount=_decimal(_text(calc) or "0"),
            )
        )

    return ParsedInvoice(
        invoice_number=invoice_number,
        issue_date=_parse_date(issue_date, fmt_hint=fmt_hint),
        seller_name=seller,
        currency_code=currency or "EUR",
        net_total=net_total,
        tax_total=tax_total,
        grand_total=grand_total,
        syntax="CII",
        tax_lines=tax_lines,
    )


def _parse_ubl(root: ET.Element) -> ParsedInvoice:
    invoice_number = ""
    issue_date = ""
    currency = ""
    # Direkte Kopf-Kinder (cbc:ID, cbc:IssueDate, cbc:DocumentCurrencyCode)
    for child in root:
        name = _local(child.tag)
        if name == "ID" and not invoice_number:
            invoice_number = _text(child)
        elif name == "IssueDate" and not issue_date:
            issue_date = _text(child)
        elif name == "DocumentCurrencyCode" and not currency:
            currency = _text(child)

    seller = ""
    supplier = _find(root, "AccountingSupplierParty", "Party")
    if supplier is not None:
        legal = _find(supplier, "PartyLegalEntity", "RegistrationName")
        name = _find(supplier, "PartyName", "Name")
        seller = _text(legal) or _text(name)

    monetary = _find(root, "LegalMonetaryTotal")
    if monetary is None:
        raise EInvoiceParseError("UBL-Rechnung ohne LegalMonetaryTotal.")
    net_total = _decimal(_text(_find(monetary, "LineExtensionAmount")) or "0")
    grand_total = _decimal(_text(_find(monetary, "TaxInclusiveAmount")) or "0")

    tax_total = Decimal("0")
    tax_lines: list[TaxBreakdown] = []
    for tax_total_el in _find_all(root, "TaxTotal"):
        amount = _find(tax_total_el, "TaxAmount")
        if amount is not None and _local(amount.tag) == "TaxAmount":
            tax_total = _decimal(_text(amount) or "0")
        for subtotal in _find_all(tax_total_el, "TaxSubtotal"):
            basis = _find(subtotal, "TaxableAmount")
            calc = _find(subtotal, "TaxAmount")
            rate = _find(subtotal, "TaxCategory", "Percent")
            tax_lines.append(
                TaxBreakdown(
                    rate=_decimal(_text(rate) or "0"),
                    basis=_decimal(_text(basis) or "0"),
                    tax_amount=_decimal(_text(calc) or "0"),
                )
            )

    if not currency:
        currency = "EUR"

    return ParsedInvoice(
        invoice_number=invoice_number,
        issue_date=_parse_date(issue_date),
        seller_name=seller,
        currency_code=currency,
        net_total=net_total,
        tax_total=tax_total,
        grand_total=grand_total,
        syntax="UBL",
        tax_lines=tax_lines,
    )


@dataclass(frozen=True, slots=True)
class RateAssignment:
    """Kontierung eines Steuersatzes der E-Rechnung (Steuercode, Aufwandskonto)."""

    rate: Decimal
    tax_code_id: int | None = None
    expense_account_id: int | None = None


def _optional_id(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def parse_rate_assignments(raw: object) -> list[RateAssignment]:
    """Liest die Kontierung je Steuersatz aus REST-/MCP-Daten (``tax_lines``).

    Erwartet eine Liste von Objekten mit ``rate`` und optional ``tax_code_id``,
    ``expense_account_id``. Beträge kommen immer aus dem XML. Fehler als
    ``ValueError``.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("tax_lines muss eine Liste sein.")
    assignments: list[RateAssignment] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("rate") in (None, ""):
            raise ValueError("Jede Zeile in tax_lines braucht einen Steuersatz (rate).")
        try:
            rate = Decimal(str(item["rate"]).replace(",", "."))
        except InvalidOperation:
            raise ValueError(f"Ungültiger Steuersatz: {item['rate']!r}") from None
        assignments.append(
            RateAssignment(
                rate=rate,
                tax_code_id=_optional_id(item.get("tax_code_id")),
                expense_account_id=_optional_id(item.get("expense_account_id")),
            )
        )
    return assignments


def einvoice_tax_splits(
    session: Session,
    invoice: ParsedInvoice,
    *,
    tax_code_id: int | None = None,
    assignments: Sequence[RateAssignment] = (),
) -> list[InvoiceTaxSplit] | None:
    """Aufteilung je Steuersatz für Rechnungen mit mehreren Sätzen, sonst ``None``.

    Steuercode je Satz: Kontierung aus ``assignments``, sonst ``tax_code_id``,
    wenn dessen Satz passt, sonst der Vorsteuer-Code zum Satz (über ``rate``
    in :func:`book_incoming_invoice`).
    """
    if len(invoice.tax_lines) < 2:
        return None
    by_rate = {assignment.rate: assignment for assignment in assignments}
    default_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
    cent = Decimal("0.01")
    splits: list[InvoiceTaxSplit] = []
    for line in invoice.tax_lines:
        assignment = by_rate.get(line.rate)
        split_tax_code_id = assignment.tax_code_id if assignment else None
        if split_tax_code_id is None and default_code is not None:
            split_tax_code_id = default_code.id if default_code.rate == line.rate else None
        splits.append(
            InvoiceTaxSplit(
                net_amount=line.basis.quantize(cent),
                tax_amount=line.tax_amount.quantize(cent),
                tax_code_id=split_tax_code_id,
                rate=line.rate,
                expense_account_id=assignment.expense_account_id if assignment else None,
            )
        )
    gross = invoice_gross_amount(Decimal("0"), Decimal("0"), splits)
    if gross != invoice.grand_total.quantize(cent):
        raise IncomingInvoiceError(
            f"Die Steueraufteilung der E-Rechnung ({format_amount(gross)}) passt nicht "
            f"zum Rechnungsbetrag ({format_amount(invoice.grand_total)}).",
            code="tax_split_mismatch",
        )
    return splits


def book_einvoice(
    *,
    session: Session,
    company: Company,
    invoice: ParsedInvoice,
    expense_account_id: int,
    creditor_account_id: int,
    tax_code_id: int | None = None,
    assignments: Sequence[RateAssignment] = (),
    cost_center_id: int | None = None,
    profit_center_id: int | None = None,
    changed_by: str,
) -> tuple[JournalEntry, list[InvoiceTaxSplit] | None]:
    """Bucht eine E-Rechnung (ohne Commit); mehrere Steuersätze je Satz.

    Bei einem Steuersatz gilt ``tax_code_id`` (bzw. die Kontierung für diesen
    Satz aus ``assignments``). Liefert die Buchung und die verwendete
    Aufteilung (``None`` bei einem Satz).
    """
    tax_splits = einvoice_tax_splits(
        session, invoice, tax_code_id=tax_code_id, assignments=assignments
    )
    if tax_splits is None and invoice.primary_tax_rate is not None:
        assignment = next(
            (a for a in assignments if a.rate == invoice.primary_tax_rate), None
        )
        if assignment is not None:
            tax_code_id = assignment.tax_code_id or tax_code_id
            expense_account_id = assignment.expense_account_id or expense_account_id
    entry = book_incoming_invoice(
        session=session,
        company=company,
        entry_date=invoice.issue_date,
        description=f"E-Rechnung {invoice.invoice_number} ({invoice.seller_name})".strip(),
        expense_account_id=expense_account_id,
        creditor_account_id=creditor_account_id,
        net_amount=invoice.net_total,
        tax_amount=invoice.tax_total,
        gross_amount=invoice.grand_total,
        tax_code_id=tax_code_id,
        expense_line_description=f"{invoice.seller_name} {invoice.invoice_number}".strip(),
        tax_line_description=f"Steuer {invoice.primary_tax_rate}%",
        cost_center_id=cost_center_id,
        profit_center_id=profit_center_id,
        tax_splits=tax_splits,
        changed_by=changed_by,
        commit=False,
    )
    return entry, tax_splits
