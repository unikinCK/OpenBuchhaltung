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

:func:`import_einvoice_booking` erkennt zusätzlich den Lieferanten (USt-IdNr., Steuernummer,
IBAN, Name) und legt den offenen Posten mit Fälligkeit an; nur eine eindeutige Erkennung wird
ohne Bestätigung zugeordnet (``partner_recognition``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.incoming_invoice import (
    IncomingInvoiceError,
    InvoiceTaxSplit,
    book_incoming_invoice,
    create_invoice_open_item,
    invoice_gross_amount,
)
from app.services.partner_identifiers import clean_bic, clean_iban
from app.services.partner_recognition import (
    PartnerMatch,
    PartnerSignals,
    load_partner_directory,
    proposed_partner_data,
)
from domain.formatting import format_amount
from domain.models import (
    SUBLEDGER_CREDITOR,
    BusinessPartner,
    Company,
    JournalEntry,
    OpenItem,
    TaxCode,
)

# Gutschriften und Stornorechnungen: Der Eingangsrechnungsweg würde sie als Rechnung mit
# positivem Betrag buchen, deshalb lehnt ``book_einvoice`` sie ab.
CREDIT_NOTE_TYPE_CODES = frozenset({"261", "381", "396", "532"})
# Grenze der Betragsspalten (Numeric(14, 2)).
MAX_INVOICE_AMOUNT = Decimal("1000000000000")


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
    # BT-3, z. B. 380 Rechnung, 381 Gutschrift/Stornorechnung; leer = nicht angegeben.
    type_code: str = ""
    # Verkäufer (BG-4) für die Partnererkennung und „Lieferant anlegen“.
    seller_vat_id: str = ""  # BT-31
    seller_tax_number: str = ""  # BT-32
    seller_identifier: str = ""  # BT-29
    seller_street: str = ""  # BT-35
    seller_postal_code: str = ""  # BT-38
    seller_city: str = ""  # BT-37
    seller_country_code: str = ""  # BT-40
    seller_email: str = ""  # BT-43
    seller_iban: str = ""  # BT-84, nur mit gültiger Prüfsumme
    seller_bic: str = ""  # BT-86
    due_date: date | None = None  # BT-9
    # BT-115; weicht vom Bruttobetrag ab, wenn schon etwas bezahlt ist (BT-113).
    due_payable_amount: Decimal | None = None

    @property
    def is_credit_note(self) -> bool:
        return self.type_code in CREDIT_NOTE_TYPE_CODES

    @property
    def amount_due(self) -> Decimal:
        """Noch zu zahlender Betrag und damit Höhe des offenen Postens.

        Nie mehr als das Gebuchte: Ein höherer BT-115 ginge sonst ungeprüft in den Zahllauf.
        """
        if self.due_payable_amount is None or self.due_payable_amount > self.grand_total:
            return self.grand_total
        return self.due_payable_amount

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
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise EInvoiceParseError(f"Ungültiger Betrag: {value!r}") from None
    if not amount.is_finite() or abs(amount) >= MAX_INVOICE_AMOUNT:
        raise EInvoiceParseError(f"Ungültiger Betrag: {value!r}")
    return amount


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


def _optional_date(value: str, *, fmt_hint: str | None = None) -> date | None:
    """Eine unlesbare Fälligkeit soll den Import der Rechnung nicht verhindern."""
    if not value.strip():
        return None
    try:
        return _parse_date(value, fmt_hint=fmt_hint)
    except EInvoiceParseError:
        return None


def _optional_decimal(value: str) -> Decimal | None:
    """Zahlbetrag oder ``None``; ``NaN``, ``1E+30`` und Negatives passen in keinen Posten."""
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and 0 <= amount < MAX_INVOICE_AMOUNT else None


def _children(element: ET.Element | None, local_name: str) -> list[ET.Element]:
    return [] if element is None else [c for c in element if _local(c.tag) == local_name]


def parse_einvoice(xml_bytes: bytes) -> ParsedInvoice:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise EInvoiceParseError(f"XML konnte nicht gelesen werden: {exc}") from exc

    root_name = _local(root.tag)
    if root_name in {"CrossIndustryInvoice", "CrossIndustryDocument"}:
        return _parse_cii(root)
    if root_name in {"Invoice", "CreditNote"}:
        return _parse_ubl(root)
    raise EInvoiceParseError(
        f"Unbekanntes Rechnungsformat (Wurzelelement {root_name!r}). "
        "Unterstützt werden CII (ZUGFeRD/XRechnung) und UBL (XRechnung)."
    )


def _cii_seller_fields(party: ET.Element | None) -> dict[str, str]:
    """Verkäuferangaben einer CII-Rechnung (``SellerTradeParty``)."""
    vat_id = tax_number = ""
    for registration in _children(party, "SpecifiedTaxRegistration"):
        identifier = _find(registration, "ID")
        scheme = identifier.get("schemeID", "") if identifier is not None else ""
        if scheme == "VA" and not vat_id:
            vat_id = _text(identifier)
        elif scheme == "FC" and not tax_number:
            tax_number = _text(identifier)
    address = _find(party, "PostalTradeAddress") if party is not None else None
    email = ""
    if party is not None:
        email = _text(_find(party, "URIUniversalCommunication", "URIID")) or _text(
            _find(party, "DefinedTradeContact", "EmailURIUniversalCommunication", "URIID")
        )
    return {
        "seller_vat_id": vat_id,
        "seller_tax_number": tax_number,
        "seller_identifier": _text(_find(party, "ID")) if party is not None else "",
        "seller_street": _text(_find(address, "LineOne")) if address is not None else "",
        "seller_postal_code": _text(_find(address, "PostcodeCode")) if address is not None else "",
        "seller_city": _text(_find(address, "CityName")) if address is not None else "",
        "seller_country_code": _text(_find(address, "CountryID")) if address is not None else "",
        "seller_email": email if "@" in email else "",
    }


def _cii_payment_fields(root: ET.Element) -> dict[str, object]:
    """Zahlungsempfänger und Fälligkeit; die Debitor-IBAN einer Lastschrift zählt nicht."""
    iban = bic = ""
    for means in _find_all(root, "SpecifiedTradeSettlementPaymentMeans"):
        iban = clean_iban(_text(_find(means, "PayeePartyCreditorFinancialAccount", "IBANID"))) or ""
        if iban:
            bic = _text(_find(means, "PayeeSpecifiedCreditorFinancialInstitution", "BICID"))
            break
    due_date = None
    for terms in _find_all(root, "SpecifiedTradePaymentTerms"):
        due = _find(terms, "DueDateDateTime", "DateTimeString")
        if due is not None:
            due_date = _optional_date(_text(due), fmt_hint=due.get("format"))
            if due_date:
                break
    return {
        "seller_iban": iban,
        "seller_bic": clean_bic(bic) or "",
        "due_date": due_date,
    }


def _parse_cii(root: ET.Element) -> ParsedInvoice:
    doc = _find(root, "ExchangedDocument")
    invoice_number = _text(_find(doc, "ID")) if doc is not None else ""
    type_code = _text(_find(doc, "TypeCode")) if doc is not None else ""

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

    # BT-5 gilt; ein zweiter TaxTotalAmount nennt nur die Steuer in Abrechnungswährung (BT-111).
    currency = _text(next(iter(_find_all(root, "InvoiceCurrencyCode")), None)) or ""
    if not currency:
        for tax_amount in _find_all(root, "TaxTotalAmount"):
            currency = currency or tax_amount.get("currencyID") or ""
    if not currency:
        currency = "EUR"

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
        type_code=type_code,
        due_payable_amount=_optional_decimal(_text(_find(summation, "DuePayableAmount"))),
        **_cii_seller_fields(seller_party),
        **_cii_payment_fields(root),
    )


def _ubl_seller_fields(party: ET.Element | None) -> dict[str, str]:
    """Verkäuferangaben einer UBL-Rechnung (``AccountingSupplierParty/Party``)."""
    vat_id = tax_number = ""
    for scheme in _children(party, "PartyTaxScheme"):
        company_id = _text(_find(scheme, "CompanyID"))
        kind = _text(_find(scheme, "TaxScheme", "ID")).upper()
        if kind == "VAT" and not vat_id:
            vat_id = company_id
        elif kind == "FC" and not tax_number:
            tax_number = company_id
    if party is None:
        return {}
    address = _find(party, "PostalAddress")
    return {
        "seller_vat_id": vat_id,
        "seller_tax_number": tax_number,
        "seller_identifier": _text(_find(party, "PartyIdentification", "ID")),
        "seller_street": _text(_find(address, "StreetName")) if address is not None else "",
        "seller_postal_code": _text(_find(address, "PostalZone")) if address is not None else "",
        "seller_city": _text(_find(address, "CityName")) if address is not None else "",
        "seller_country_code": (
            _text(_find(address, "Country", "IdentificationCode")) if address is not None else ""
        ),
        "seller_email": _text(_find(party, "Contact", "ElectronicMail")),
    }


def _ubl_payment_fields(root: ET.Element) -> dict[str, object]:
    """Zahlungsempfänger und Fälligkeit (Gutschriften nennen sie unter ``PaymentMeans``)."""
    iban = bic = ""
    due_text = _text(_find(root, "DueDate"))
    for means in _children(root, "PaymentMeans"):
        due_text = due_text or _text(_find(means, "PaymentDueDate"))
        account = _find(means, "PayeeFinancialAccount")
        if account is not None and not iban:
            iban = clean_iban(_text(_find(account, "ID"))) or ""
            if iban:
                bic = _text(_find(account, "FinancialInstitutionBranch", "ID"))
    return {
        "seller_iban": iban,
        "seller_bic": clean_bic(bic) or "",
        "due_date": _optional_date(due_text),
    }


def _parse_ubl(root: ET.Element) -> ParsedInvoice:
    invoice_number = ""
    issue_date = ""
    currency = ""
    type_code = ""
    # Direkte Kopf-Kinder (cbc:ID, cbc:IssueDate, cbc:DocumentCurrencyCode)
    for child in root:
        name = _local(child.tag)
        if name == "ID" and not invoice_number:
            invoice_number = _text(child)
        elif name == "IssueDate" and not issue_date:
            issue_date = _text(child)
        elif name == "DocumentCurrencyCode" and not currency:
            currency = _text(child)
        elif name in {"InvoiceTypeCode", "CreditNoteTypeCode"} and not type_code:
            type_code = _text(child)

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
        type_code=type_code,
        due_payable_amount=_optional_decimal(_text(_find(monetary, "PayableAmount"))),
        **_ubl_seller_fields(supplier),
        **_ubl_payment_fields(root),
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
    partner_id: int | None = None,
    changed_by: str,
) -> tuple[JournalEntry, list[InvoiceTaxSplit] | None]:
    """Bucht eine E-Rechnung (ohne Commit); mehrere Steuersätze je Satz.

    Bei einem Steuersatz gilt ``tax_code_id`` (bzw. die Kontierung für diesen
    Satz aus ``assignments``). Liefert die Buchung und die verwendete
    Aufteilung (``None`` bei einem Satz). Gutschriften werden abgelehnt.
    """
    if invoice.is_credit_note:
        raise IncomingInvoiceError(
            f"{invoice.invoice_number} ist eine Gutschrift bzw. Stornorechnung "
            f"(Typ {invoice.type_code}) und kann nicht als Eingangsrechnung gebucht werden. "
            "Bitte manuell buchen.",
            code="credit_note",
        )
    # Die Beträge der Rechnung gingen sonst 1:1 als Euro ins Hauptbuch.
    if invoice.currency_code != company.currency_code:
        raise IncomingInvoiceError(
            f"{invoice.invoice_number} läuft in {invoice.currency_code}. E-Rechnungen werden nur "
            f"in {company.currency_code} automatisch gebucht. Bitte manuell mit Kurs buchen.",
            code="foreign_currency",
        )
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
        description=_einvoice_description(invoice),
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
        partner_id=partner_id,
        tax_splits=tax_splits,
        changed_by=changed_by,
        commit=False,
    )
    return entry, tax_splits


def invoice_amount_warnings(invoice: ParsedInvoice) -> list[str]:
    """Ein Zahlbetrag, der vom Brutto abweicht, soll jemand sehen, bevor er bezahlt wird."""
    due = invoice.due_payable_amount
    if due is None or due == invoice.grand_total:
        return []
    if due > invoice.grand_total:
        return [
            f"Der Zahlbetrag der Rechnung ({format_amount(due)}) liegt über dem Bruttobetrag "
            f"({format_amount(invoice.grand_total)}). Der offene Posten nennt den Bruttobetrag."
        ]
    return [
        f"Der Zahlbetrag der Rechnung ({format_amount(due)}) liegt unter dem Bruttobetrag "
        f"({format_amount(invoice.grand_total)}), laut Rechnung ist schon etwas bezahlt."
    ]


def _einvoice_description(invoice: ParsedInvoice) -> str:
    return f"E-Rechnung {invoice.invoice_number} ({invoice.seller_name})".strip()


def find_booked_einvoice(
    session: Session,
    company_id: int,
    invoice: ParsedInvoice,
    partner_id: int | None = None,
) -> JournalEntry | None:
    """Nicht stornierte Buchung derselben Rechnung im selben Kalenderjahr.

    Gleich heißt: dieselbe Nummer vom selben Absender, ohne Groß-/Kleinschreibung, oder dieselbe
    Nummer auf einem Posten desselben Partners, wenn der Absender anders geschrieben ist. Das
    Kalenderjahr gehört dazu, weil manche Lieferanten ihren Nummernkreis jährlich neu beginnen.
    """
    if not invoice.invoice_number:
        return None
    reversed_ids = select(JournalEntry.reversal_of_id).where(
        JournalEntry.company_id == company_id, JournalEntry.reversal_of_id.is_not(None)
    )
    same_invoice = [
        JournalEntry.company_id == company_id,
        JournalEntry.reversal_of_id.is_(None),
        JournalEntry.id.not_in(reversed_ids),
        JournalEntry.entry_date >= date(invoice.issue_date.year, 1, 1),
        JournalEntry.entry_date <= date(invoice.issue_date.year, 12, 31),
    ]
    if invoice.seller_name:
        found = (
            session.execute(
                select(JournalEntry)
                .where(
                    *same_invoice,
                    func.lower(JournalEntry.description)
                    == _einvoice_description(invoice).lower(),
                )
                .order_by(JournalEntry.id)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if found is not None:
            return found
    if partner_id is None:
        return None
    return (
        session.execute(
            select(JournalEntry)
            .join(OpenItem, OpenItem.journal_entry_id == JournalEntry.id)
            .where(
                *same_invoice,
                OpenItem.partner_id == partner_id,
                OpenItem.item_type == "payable",
                func.lower(OpenItem.reference) == invoice.invoice_number.lower(),
            )
            .order_by(JournalEntry.id)
            .limit(1)
        )
        .scalars()
        .first()
    )


def einvoice_partner_signals(invoice: ParsedInvoice) -> PartnerSignals:
    """Was die Rechnung über ihren Absender verrät; BT-29 bleibt ein bloßer Hinweis."""
    return PartnerSignals(
        vat_ids=(invoice.seller_vat_id,) if invoice.seller_vat_id else (),
        tax_numbers=(invoice.seller_tax_number,) if invoice.seller_tax_number else (),
        ibans=(invoice.seller_iban,) if invoice.seller_iban else (),
        number_hints=(invoice.seller_identifier,) if invoice.seller_identifier else (),
        name=invoice.seller_name,
    )


def recognize_einvoice_partner(
    session: Session, company_id: int, invoice: ParsedInvoice
) -> PartnerMatch:
    """Lieferant der Rechnung; eine Eingangsrechnung verlangt die Lieferantenrolle."""
    directory = load_partner_directory(session, company_id)
    return directory.match(einvoice_partner_signals(invoice), role=SUBLEDGER_CREDITOR)


def proposed_partner_for_invoice(invoice: ParsedInvoice) -> dict[str, object]:
    """Vorbelegung für „Lieferant anlegen“ aus den Verkäuferangaben."""
    return proposed_partner_data(
        name=invoice.seller_name,
        vat_id=invoice.seller_vat_id,
        tax_number=invoice.seller_tax_number,
        street=invoice.seller_street,
        postal_code=invoice.seller_postal_code,
        city=invoice.seller_city,
        country_code=invoice.seller_country_code,
        email=invoice.seller_email,
        iban=invoice.seller_iban,
        bic=invoice.seller_bic,
    )


@dataclass(slots=True)
class EInvoiceBooking:
    entry: JournalEntry
    tax_splits: list[InvoiceTaxSplit] | None
    partner_match: PartnerMatch
    partner_id: int | None
    open_item: OpenItem | None
    warnings: list[str]


def import_einvoice_booking(
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
    partner_id: int | None = None,
    auto_partner: bool = True,
    create_open_item: bool = True,
    changed_by: str,
) -> EInvoiceBooking:
    """Bucht die Rechnung mit Partner und offenem Posten (ohne Commit).

    ``partner_id`` ist die bestätigte Wahl. Ohne sie gilt mit ``auto_partner`` nur eine eindeutige
    Erkennung; ein unsicherer Treffer bleibt ein Vorschlag, die Buchung läuft dann ohne Partner.
    """
    directory = load_partner_directory(session, company.id)
    signals = einvoice_partner_signals(invoice)
    match = directory.match(signals, role=SUBLEDGER_CREDITOR)
    if partner_id is None and auto_partner and match.partner is not None:
        partner_id = match.partner.id
    duplicate = find_booked_einvoice(
        session,
        company.id,
        invoice,
        partner_id=partner_id or (match.partner.id if match.partner else None),
    )
    if duplicate is not None:
        raise IncomingInvoiceError(
            f"E-Rechnung {invoice.invoice_number} ist bereits als "
            f"{duplicate.posting_number} gebucht. Zum erneuten Buchen zuerst stornieren.",
            code="duplicate_invoice",
        )
    entry, tax_splits = book_einvoice(
        session=session,
        company=company,
        invoice=invoice,
        expense_account_id=expense_account_id,
        creditor_account_id=creditor_account_id,
        tax_code_id=tax_code_id,
        assignments=assignments,
        cost_center_id=cost_center_id,
        profit_center_id=profit_center_id,
        partner_id=partner_id,
        changed_by=changed_by,
    )
    partner = session.get(BusinessPartner, partner_id) if partner_id else None
    warnings = list(match.warnings)
    if partner is not None and (match.partner is None or match.partner.id != partner.id):
        warnings = list(directory.identifier_warnings(signals, partner))
    warnings.extend(invoice_amount_warnings(invoice))
    # Kommt ein bekannter Partner infrage, ist aber keiner gewählt, zählt die IBAN der Rechnung
    # nicht: Sie stünde ungeprüft im Zahllauf (siehe create_invoice_open_item).
    invoice_iban = invoice.seller_iban
    if partner is None and match.candidates and invoice_iban:
        invoice_iban = ""
        warnings.append(
            "Die IBAN der Rechnung wurde nicht auf den offenen Posten übernommen, weil der Partner "
            "noch nicht bestätigt ist."
        )
    open_item = None
    if create_open_item:
        open_item = create_invoice_open_item(
            session=session,
            company=company,
            entry=entry,
            creditor_account_id=creditor_account_id,
            amount=invoice.amount_due.quantize(Decimal("0.01")),
            reference=invoice.invoice_number,
            partner_id=partner_id,
            counterparty=invoice.seller_name,
            due_date=invoice.due_date,
            iban=invoice_iban,
            bic=invoice.seller_bic if invoice_iban else None,
            changed_by=changed_by,
        )
    return EInvoiceBooking(
        entry=entry,
        tax_splits=tax_splits,
        partner_match=match,
        partner_id=partner_id,
        open_item=open_item,
        warnings=warnings,
    )


def preview_einvoice_payload(
    session: Session, company_id: int, invoice: ParsedInvoice
) -> dict[str, object]:
    """Ergebnis der Prüfung vor dem Buchen (Oberfläche, REST und MCP)."""
    match = recognize_einvoice_partner(session, company_id, invoice)
    warnings = invoice_amount_warnings(invoice)
    duplicate = find_booked_einvoice(
        session, company_id, invoice, partner_id=match.partner.id if match.partner else None
    )
    if duplicate is not None:
        warnings.insert(
            0,
            f"Diese Rechnung ist bereits als {duplicate.posting_number} gebucht und lässt sich "
            "erst nach deren Storno erneut buchen.",
        )
    return {
        "partner_match": match.as_dict(),
        "warnings": warnings,
        # Nur ohne eindeutigen Treffer sinnvoll: sonst entstünde eine Dublette.
        "proposed_partner": (
            None if match.partner is not None else proposed_partner_for_invoice(invoice)
        ),
    }
