"""Import strukturierter elektronischer Rechnungen (XRechnung / ZUGFeRD).

Unterstützt beide in Deutschland relevanten XML-Syntaxen:
    * UN/CEFACT CII (Cross Industry Invoice) — u. a. ZUGFeRD und XRechnung-CII
    * OASIS UBL (Universal Business Language) — XRechnung-UBL

Der Parser arbeitet namespace-agnostisch (nur über die lokalen Elementnamen),
damit er mit den verschiedenen Namespace-Versionen der Formate umgeht. Er liefert
die für eine Verbuchung nötigen Kopfdaten (Rechnungsnummer, Datum, Lieferant,
Netto/Steuer/Brutto und die Steuersätze).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET


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
        rate = _find(trade_tax, "RateApplicablePercent")
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
