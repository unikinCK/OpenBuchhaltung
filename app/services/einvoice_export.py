"""Export von Ausgangsrechnungen als elektronische Rechnung (XRechnung / ZUGFeRD).

Erzeugt gültiges XML in beiden in Deutschland relevanten Syntaxen:
    * OASIS UBL (XRechnung-UBL)
    * UN/CEFACT CII (ZUGFeRD / XRechnung-CII)

Das Gegenstück zum Import (``einvoice_import``). Beträge und Steueraufteilung
werden aus den Rechnungspositionen berechnet; Positionen werden je Steuersatz
zu einer Steuerkategorie zusammengefasst.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from xml.etree import ElementTree as ET

CENT = Decimal("0.01")

UBL_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

RSM_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM_NS = (
    "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
)
UDT_NS = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"


class EInvoiceExportError(ValueError):
    """Raised when an outgoing invoice cannot be built."""


@dataclass(slots=True)
class Party:
    name: str
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country_code: str = "DE"
    vat_id: str = ""


@dataclass(slots=True)
class InvoiceLine:
    name: str
    quantity: Decimal
    unit_price: Decimal  # Nettopreis je Einheit
    tax_rate: Decimal

    @property
    def net_amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class TaxGroup:
    rate: Decimal
    basis: Decimal
    tax_amount: Decimal


@dataclass(slots=True)
class OutgoingInvoice:
    invoice_number: str
    issue_date: date
    seller: Party
    buyer: Party
    lines: list[InvoiceLine]
    currency_code: str = "EUR"
    buyer_reference: str = ""  # Leitweg-ID (Pflicht bei öffentlichen Auftraggebern)

    def __post_init__(self) -> None:
        if not self.lines:
            raise EInvoiceExportError("Rechnung braucht mindestens eine Position.")
        if not self.invoice_number.strip():
            raise EInvoiceExportError("Rechnungsnummer fehlt.")

    @property
    def tax_groups(self) -> list[TaxGroup]:
        basis_by_rate: dict[Decimal, Decimal] = {}
        for line in self.lines:
            basis_by_rate[line.tax_rate] = basis_by_rate.get(line.tax_rate, Decimal("0")) + (
                line.net_amount
            )
        groups: list[TaxGroup] = []
        for rate in sorted(basis_by_rate):
            basis = basis_by_rate[rate].quantize(CENT)
            tax = (basis * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
            groups.append(TaxGroup(rate=rate, basis=basis, tax_amount=tax))
        return groups

    @property
    def net_total(self) -> Decimal:
        return sum((line.net_amount for line in self.lines), Decimal("0")).quantize(CENT)

    @property
    def tax_total(self) -> Decimal:
        return sum((group.tax_amount for group in self.tax_groups), Decimal("0")).quantize(CENT)

    @property
    def grand_total(self) -> Decimal:
        return (self.net_total + self.tax_total).quantize(CENT)


def _amount(value: Decimal) -> str:
    return f"{value.quantize(CENT):.2f}"


def _rate(value: Decimal) -> str:
    return f"{value:.2f}"


def _to_xml(root: ET.Element) -> str:
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


# --- UBL ---------------------------------------------------------------------


def _ubl_sub(parent: ET.Element, ns: str, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ns}}}{tag}", {k: v for k, v in attrs.items()})
    if text is not None:
        el.text = text
    return el


def _ubl_party(parent: ET.Element, wrapper: str, party: Party, currency: str) -> None:
    del currency
    supplier = _ubl_sub(parent, CAC_NS, wrapper)
    party_el = _ubl_sub(supplier, CAC_NS, "Party")
    name_el = _ubl_sub(party_el, CAC_NS, "PartyName")
    _ubl_sub(name_el, CBC_NS, "Name", party.name)
    address = _ubl_sub(party_el, CAC_NS, "PostalAddress")
    _ubl_sub(address, CBC_NS, "StreetName", party.street)
    _ubl_sub(address, CBC_NS, "CityName", party.city)
    _ubl_sub(address, CBC_NS, "PostalZone", party.postal_code)
    country = _ubl_sub(address, CAC_NS, "Country")
    _ubl_sub(country, CBC_NS, "IdentificationCode", party.country_code)
    if party.vat_id:
        tax_scheme = _ubl_sub(party_el, CAC_NS, "PartyTaxScheme")
        _ubl_sub(tax_scheme, CBC_NS, "CompanyID", party.vat_id)
        scheme = _ubl_sub(tax_scheme, CAC_NS, "TaxScheme")
        _ubl_sub(scheme, CBC_NS, "ID", "VAT")
    legal = _ubl_sub(party_el, CAC_NS, "PartyLegalEntity")
    _ubl_sub(legal, CBC_NS, "RegistrationName", party.name)


def build_ubl(invoice: OutgoingInvoice) -> str:
    ET.register_namespace("", UBL_NS)
    ET.register_namespace("cac", CAC_NS)
    ET.register_namespace("cbc", CBC_NS)
    cur = invoice.currency_code

    def money(parent: ET.Element, tag: str, value: Decimal) -> None:
        _ubl_sub(parent, CBC_NS, tag, _amount(value), currencyID=cur)

    root = ET.Element(f"{{{UBL_NS}}}Invoice")
    _ubl_sub(root, CBC_NS, "CustomizationID", "urn:cen.eu:en16931:2017")
    _ubl_sub(root, CBC_NS, "ID", invoice.invoice_number)
    _ubl_sub(root, CBC_NS, "IssueDate", invoice.issue_date.isoformat())
    _ubl_sub(root, CBC_NS, "InvoiceTypeCode", "380")
    _ubl_sub(root, CBC_NS, "DocumentCurrencyCode", cur)
    if invoice.buyer_reference:
        _ubl_sub(root, CBC_NS, "BuyerReference", invoice.buyer_reference)

    _ubl_party(root, "AccountingSupplierParty", invoice.seller, cur)
    _ubl_party(root, "AccountingCustomerParty", invoice.buyer, cur)

    tax_total = _ubl_sub(root, CAC_NS, "TaxTotal")
    money(tax_total, "TaxAmount", invoice.tax_total)
    for group in invoice.tax_groups:
        subtotal = _ubl_sub(tax_total, CAC_NS, "TaxSubtotal")
        money(subtotal, "TaxableAmount", group.basis)
        money(subtotal, "TaxAmount", group.tax_amount)
        category = _ubl_sub(subtotal, CAC_NS, "TaxCategory")
        _ubl_sub(category, CBC_NS, "ID", "S" if group.rate > 0 else "Z")
        _ubl_sub(category, CBC_NS, "Percent", _rate(group.rate))
        scheme = _ubl_sub(category, CAC_NS, "TaxScheme")
        _ubl_sub(scheme, CBC_NS, "ID", "VAT")

    monetary = _ubl_sub(root, CAC_NS, "LegalMonetaryTotal")
    money(monetary, "LineExtensionAmount", invoice.net_total)
    money(monetary, "TaxExclusiveAmount", invoice.net_total)
    money(monetary, "TaxInclusiveAmount", invoice.grand_total)
    money(monetary, "PayableAmount", invoice.grand_total)

    for index, line in enumerate(invoice.lines, start=1):
        line_el = _ubl_sub(root, CAC_NS, "InvoiceLine")
        _ubl_sub(line_el, CBC_NS, "ID", str(index))
        _ubl_sub(line_el, CBC_NS, "InvoicedQuantity", _amount(line.quantity), unitCode="C62")
        money(line_el, "LineExtensionAmount", line.net_amount)
        item = _ubl_sub(line_el, CAC_NS, "Item")
        _ubl_sub(item, CBC_NS, "Name", line.name)
        category = _ubl_sub(item, CAC_NS, "ClassifiedTaxCategory")
        _ubl_sub(category, CBC_NS, "ID", "S" if line.tax_rate > 0 else "Z")
        _ubl_sub(category, CBC_NS, "Percent", _rate(line.tax_rate))
        scheme = _ubl_sub(category, CAC_NS, "TaxScheme")
        _ubl_sub(scheme, CBC_NS, "ID", "VAT")
        price = _ubl_sub(line_el, CAC_NS, "Price")
        money(price, "PriceAmount", line.unit_price)

    return _to_xml(root)


# --- CII ---------------------------------------------------------------------


def _cii_sub(parent: ET.Element, ns: str, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ns}}}{tag}", {k: v for k, v in attrs.items()})
    if text is not None:
        el.text = text
    return el


def build_cii(invoice: OutgoingInvoice) -> str:
    ET.register_namespace("rsm", RSM_NS)
    ET.register_namespace("ram", RAM_NS)
    ET.register_namespace("udt", UDT_NS)

    root = ET.Element(f"{{{RSM_NS}}}CrossIndustryInvoice")

    doc = _cii_sub(root, RSM_NS, "ExchangedDocument")
    _cii_sub(doc, RAM_NS, "ID", invoice.invoice_number)
    _cii_sub(doc, RAM_NS, "TypeCode", "380")
    issue = _cii_sub(doc, RAM_NS, "IssueDateTime")
    _cii_sub(issue, UDT_NS, "DateTimeString", invoice.issue_date.strftime("%Y%m%d"), format="102")

    transaction = _cii_sub(root, RSM_NS, "SupplyChainTradeTransaction")

    agreement = _cii_sub(transaction, RAM_NS, "ApplicableHeaderTradeAgreement")
    if invoice.buyer_reference:
        _cii_sub(agreement, RAM_NS, "BuyerReference", invoice.buyer_reference)
    seller = _cii_sub(agreement, RAM_NS, "SellerTradeParty")
    _cii_sub(seller, RAM_NS, "Name", invoice.seller.name)
    if invoice.seller.vat_id:
        tax_reg = _cii_sub(seller, RAM_NS, "SpecifiedTaxRegistration")
        _cii_sub(tax_reg, RAM_NS, "ID", invoice.seller.vat_id, schemeID="VA")
    buyer = _cii_sub(agreement, RAM_NS, "BuyerTradeParty")
    _cii_sub(buyer, RAM_NS, "Name", invoice.buyer.name)

    settlement = _cii_sub(transaction, RAM_NS, "ApplicableHeaderTradeSettlement")
    _cii_sub(settlement, RAM_NS, "InvoiceCurrencyCode", invoice.currency_code)
    for group in invoice.tax_groups:
        trade_tax = _cii_sub(settlement, RAM_NS, "ApplicableTradeTax")
        _cii_sub(trade_tax, RAM_NS, "CalculatedAmount", _amount(group.tax_amount))
        _cii_sub(trade_tax, RAM_NS, "TypeCode", "VAT")
        _cii_sub(trade_tax, RAM_NS, "BasisAmount", _amount(group.basis))
        _cii_sub(trade_tax, RAM_NS, "CategoryCode", "S" if group.rate > 0 else "Z")
        _cii_sub(trade_tax, RAM_NS, "RateApplicablePercent", _rate(group.rate))
    summation = _cii_sub(
        settlement, RAM_NS, "SpecifiedTradeSettlementHeaderMonetarySummation"
    )
    _cii_sub(summation, RAM_NS, "LineTotalAmount", _amount(invoice.net_total))
    _cii_sub(summation, RAM_NS, "TaxBasisTotalAmount", _amount(invoice.net_total))
    _cii_sub(
        summation,
        RAM_NS,
        "TaxTotalAmount",
        _amount(invoice.tax_total),
        currencyID=invoice.currency_code,
    )
    _cii_sub(summation, RAM_NS, "GrandTotalAmount", _amount(invoice.grand_total))
    _cii_sub(summation, RAM_NS, "DuePayableAmount", _amount(invoice.grand_total))

    return _to_xml(root)


def build_einvoice(invoice: OutgoingInvoice, *, syntax: str) -> str:
    if syntax == "ubl":
        return build_ubl(invoice)
    if syntax == "cii":
        return build_cii(invoice)
    raise EInvoiceExportError(f"Unbekannte Syntax: {syntax!r} (erwartet 'ubl' oder 'cii').")
