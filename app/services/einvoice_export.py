"""Export von Ausgangsrechnungen als elektronische Rechnung (XRechnung / ZUGFeRD).

Erzeugt XML in beiden in Deutschland relevanten Syntaxen:
    * OASIS UBL (XRechnung-UBL), Stornorechnungen (Typ 381) als ``CreditNote``
    * UN/CEFACT CII (ZUGFeRD / XRechnung-CII)

Das Gegenstück zum Import (``einvoice_import``). Beträge und Steueraufteilung
werden aus den Rechnungspositionen berechnet; Positionen werden je Steuersatz
zu einer Steuerkategorie zusammengefasst. Die Elementreihenfolge folgt den
Schemata; gegen den KoSIT-Validator geprüft ist die Ausgabe nicht (siehe README).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from xml.etree import ElementTree as ET

CENT = Decimal("0.01")

UBL_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE_NS = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

RSM_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM_NS = (
    "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
)
UDT_NS = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
QDT_NS = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"

for _prefix, _uri in (
    ("ubl", UBL_NS),
    ("cn", UBL_CREDIT_NOTE_NS),
    ("cac", CAC_NS),
    ("cbc", CBC_NS),
    ("rsm", RSM_NS),
    ("ram", RAM_NS),
    ("udt", UDT_NS),
    ("qdt", QDT_NS),
):
    ET.register_namespace(_prefix, _uri)

# BT-24 und BT-23: XRechnung 3.0 auf Basis von EN 16931, Geschäftsprozess nach Peppol.
XRECHNUNG_CUSTOMIZATION_ID = (
    "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
)
XRECHNUNG_PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

INVOICE_TYPE_COMMERCIAL = "380"
INVOICE_TYPE_CREDIT_NOTE = "381"
# BG-16: SEPA-Überweisung.
PAYMENT_MEANS_SEPA_CREDIT_TRANSFER = "58"
DEFAULT_UNIT_CODE = "C62"


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
    # BT-32 Steuernummer des Verkäufers.
    tax_number: str = ""
    # BG-6 Kontakt des Verkäufers bzw. Ansprechpartner des Käufers.
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    # BT-30 Registernummer und BT-33 weitere rechtliche Angaben des Verkäufers.
    legal_registration_id: str = ""
    legal_form_text: str = ""


@dataclass(slots=True)
class InvoiceLine:
    name: str
    quantity: Decimal
    unit_price: Decimal  # Nettopreis je Einheit
    tax_rate: Decimal
    unit_code: str = DEFAULT_UNIT_CODE

    @property
    def net_amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class TaxGroup:
    rate: Decimal
    basis: Decimal
    tax_amount: Decimal


def compute_tax_groups(lines: Iterable[tuple[Decimal, Decimal]]) -> list[TaxGroup]:
    """Steueraufteilung aus ``(Steuersatz, Nettobetrag)`` je Position.

    Die Steuer wird je Satz aus der Summe gerundet (EN 16931, BR-S-09), nicht je Position.
    Rechnung, Buchung, XML und PDF rechnen damit identisch.
    """
    basis_by_rate: dict[Decimal, Decimal] = {}
    for rate, net_amount in lines:
        basis_by_rate[rate] = basis_by_rate.get(rate, Decimal("0")) + net_amount
    groups: list[TaxGroup] = []
    for rate in sorted(basis_by_rate):
        basis = basis_by_rate[rate].quantize(CENT)
        tax = (basis * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        groups.append(TaxGroup(rate=rate, basis=basis, tax_amount=tax))
    return groups


@dataclass(slots=True)
class OutgoingInvoice:
    invoice_number: str
    issue_date: date
    seller: Party
    buyer: Party
    lines: list[InvoiceLine]
    currency_code: str = "EUR"
    buyer_reference: str = ""  # Leitweg-ID (Pflicht bei öffentlichen Auftraggebern)
    # BT-3: 380 Rechnung, 381 Gutschrift/Stornorechnung (UBL: Wurzel ``CreditNote``).
    type_code: str = INVOICE_TYPE_COMMERCIAL
    due_date: date | None = None  # BT-9
    service_date: date | None = None  # BT-72
    service_period_from: date | None = None  # BT-73
    service_period_to: date | None = None  # BT-74
    # BT-118/BT-151: Kategorie aller Positionen; leer = S bzw. Z aus dem Steuersatz.
    tax_category: str = ""
    exemption_reason: str = ""  # BT-120
    exemption_reason_code: str = ""  # BT-121
    # BG-16/BG-17: Zahlungsart mit IBAN; BT-83 Verwendungszweck, BT-20 Zahlungsbedingungen.
    payment_iban: str = ""
    payment_bic: str = ""
    payment_account_name: str = ""
    payment_reference: str = ""
    payment_terms: str = ""
    # BT-22: freie Bemerkungen.
    notes: list[str] = field(default_factory=list)
    # BT-25/BT-26: Bezug auf die stornierte Rechnung.
    preceding_invoice_number: str = ""
    preceding_invoice_date: date | None = None

    def __post_init__(self) -> None:
        if not self.lines:
            raise EInvoiceExportError("Rechnung braucht mindestens eine Position.")
        if not self.invoice_number.strip():
            raise EInvoiceExportError("Rechnungsnummer fehlt.")

    @property
    def is_credit_note(self) -> bool:
        return self.type_code == INVOICE_TYPE_CREDIT_NOTE

    def category_for(self, rate: Decimal) -> str:
        return self.tax_category or ("S" if rate > 0 else "Z")

    @property
    def tax_groups(self) -> list[TaxGroup]:
        return compute_tax_groups((line.tax_rate, line.net_amount) for line in self.lines)

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


def _decimal_text(value: Decimal) -> str:
    """Menge oder Preis ohne Rundung, mindestens zwei Nachkommastellen."""
    exponent = value.normalize().as_tuple().exponent
    places = max(2, -exponent if isinstance(exponent, int) else 2)
    return f"{value:.{places}f}"


def _to_xml(root: ET.Element) -> str:
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def _sub(parent: ET.Element, ns: str, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ns}}}{tag}", {k: v for k, v in attrs.items()})
    if text is not None:
        el.text = text
    return el


def _sub_if(parent: ET.Element, ns: str, tag: str, text: str | None, **attrs) -> None:
    """Element nur mit Inhalt anlegen: leere Elemente sind in den Schemata nicht vorgesehen."""
    if text:
        _sub(parent, ns, tag, text, **attrs)


# --- UBL ---------------------------------------------------------------------


def _ubl_party(parent: ET.Element, wrapper: str, party: Party) -> None:
    supplier = _sub(parent, CAC_NS, wrapper)
    party_el = _sub(supplier, CAC_NS, "Party")
    _sub_if(party_el, CBC_NS, "EndpointID", party.email, schemeID="EM")
    name_el = _sub(party_el, CAC_NS, "PartyName")
    _sub(name_el, CBC_NS, "Name", party.name)
    address = _sub(party_el, CAC_NS, "PostalAddress")
    _sub_if(address, CBC_NS, "StreetName", party.street)
    _sub_if(address, CBC_NS, "CityName", party.city)
    _sub_if(address, CBC_NS, "PostalZone", party.postal_code)
    country = _sub(address, CAC_NS, "Country")
    _sub(country, CBC_NS, "IdentificationCode", party.country_code)
    if party.vat_id:
        tax_scheme = _sub(party_el, CAC_NS, "PartyTaxScheme")
        _sub(tax_scheme, CBC_NS, "CompanyID", party.vat_id)
        scheme = _sub(tax_scheme, CAC_NS, "TaxScheme")
        _sub(scheme, CBC_NS, "ID", "VAT")
    if party.tax_number:
        tax_scheme = _sub(party_el, CAC_NS, "PartyTaxScheme")
        _sub(tax_scheme, CBC_NS, "CompanyID", party.tax_number)
        scheme = _sub(tax_scheme, CAC_NS, "TaxScheme")
        _sub(scheme, CBC_NS, "ID", "FC")
    legal = _sub(party_el, CAC_NS, "PartyLegalEntity")
    _sub(legal, CBC_NS, "RegistrationName", party.name)
    _sub_if(legal, CBC_NS, "CompanyID", party.legal_registration_id)
    _sub_if(legal, CBC_NS, "CompanyLegalForm", party.legal_form_text)
    if party.contact_name or party.phone or party.email:
        contact = _sub(party_el, CAC_NS, "Contact")
        _sub_if(contact, CBC_NS, "Name", party.contact_name)
        _sub_if(contact, CBC_NS, "Telephone", party.phone)
        _sub_if(contact, CBC_NS, "ElectronicMail", party.email)


def _ubl_tax_category(
    parent: ET.Element, wrapper: str, invoice: OutgoingInvoice, rate: Decimal, *, with_reason: bool
) -> None:
    category = _sub(parent, CAC_NS, wrapper)
    _sub(category, CBC_NS, "ID", invoice.category_for(rate))
    _sub(category, CBC_NS, "Percent", _rate(rate))
    if with_reason:
        _sub_if(category, CBC_NS, "TaxExemptionReasonCode", invoice.exemption_reason_code)
        _sub_if(category, CBC_NS, "TaxExemptionReason", invoice.exemption_reason)
    scheme = _sub(category, CAC_NS, "TaxScheme")
    _sub(scheme, CBC_NS, "ID", "VAT")


def _ubl_payment(root: ET.Element, invoice: OutgoingInvoice) -> None:
    if invoice.payment_iban:
        means = _sub(root, CAC_NS, "PaymentMeans")
        _sub(means, CBC_NS, "PaymentMeansCode", PAYMENT_MEANS_SEPA_CREDIT_TRANSFER)
        if invoice.is_credit_note and invoice.due_date is not None:
            # CreditNote kennt kein DueDate im Kopf, BT-9 steht hier.
            _sub(means, CBC_NS, "PaymentDueDate", invoice.due_date.isoformat())
        _sub_if(means, CBC_NS, "PaymentID", invoice.payment_reference)
        account = _sub(means, CAC_NS, "PayeeFinancialAccount")
        _sub(account, CBC_NS, "ID", invoice.payment_iban)
        _sub_if(account, CBC_NS, "Name", invoice.payment_account_name)
        if invoice.payment_bic:
            branch = _sub(account, CAC_NS, "FinancialInstitutionBranch")
            _sub(branch, CBC_NS, "ID", invoice.payment_bic)
    if invoice.payment_terms:
        terms = _sub(root, CAC_NS, "PaymentTerms")
        _sub(terms, CBC_NS, "Note", invoice.payment_terms)


def build_ubl(invoice: OutgoingInvoice) -> str:
    cur = invoice.currency_code
    credit_note = invoice.is_credit_note

    def money(parent: ET.Element, tag: str, value: Decimal) -> None:
        _sub(parent, CBC_NS, tag, _amount(value), currencyID=cur)

    root = ET.Element(f"{{{UBL_CREDIT_NOTE_NS if credit_note else UBL_NS}}}"
                      f"{'CreditNote' if credit_note else 'Invoice'}")
    _sub(root, CBC_NS, "CustomizationID", XRECHNUNG_CUSTOMIZATION_ID)
    _sub(root, CBC_NS, "ProfileID", XRECHNUNG_PROFILE_ID)
    _sub(root, CBC_NS, "ID", invoice.invoice_number)
    _sub(root, CBC_NS, "IssueDate", invoice.issue_date.isoformat())
    if invoice.due_date is not None and not credit_note:
        _sub(root, CBC_NS, "DueDate", invoice.due_date.isoformat())
    _sub(
        root,
        CBC_NS,
        "CreditNoteTypeCode" if credit_note else "InvoiceTypeCode",
        invoice.type_code,
    )
    for note in invoice.notes:
        _sub_if(root, CBC_NS, "Note", note)
    _sub(root, CBC_NS, "DocumentCurrencyCode", cur)
    _sub_if(root, CBC_NS, "BuyerReference", invoice.buyer_reference)

    if invoice.service_period_from is not None or invoice.service_period_to is not None:
        period = _sub(root, CAC_NS, "InvoicePeriod")
        if invoice.service_period_from is not None:
            _sub(period, CBC_NS, "StartDate", invoice.service_period_from.isoformat())
        if invoice.service_period_to is not None:
            _sub(period, CBC_NS, "EndDate", invoice.service_period_to.isoformat())

    if invoice.preceding_invoice_number:
        billing = _sub(root, CAC_NS, "BillingReference")
        reference = _sub(billing, CAC_NS, "InvoiceDocumentReference")
        _sub(reference, CBC_NS, "ID", invoice.preceding_invoice_number)
        if invoice.preceding_invoice_date is not None:
            _sub(reference, CBC_NS, "IssueDate", invoice.preceding_invoice_date.isoformat())

    _ubl_party(root, "AccountingSupplierParty", invoice.seller)
    _ubl_party(root, "AccountingCustomerParty", invoice.buyer)

    ships_abroad = invoice.tax_category == "K"
    if invoice.service_date is not None or ships_abroad:
        delivery = _sub(root, CAC_NS, "Delivery")
        if invoice.service_date is not None:
            _sub(delivery, CBC_NS, "ActualDeliveryDate", invoice.service_date.isoformat())
        if ships_abroad:
            # BR-IC-12: Lieferland bei innergemeinschaftlichen Lieferungen.
            location = _sub(delivery, CAC_NS, "DeliveryLocation")
            address = _sub(location, CAC_NS, "Address")
            country = _sub(address, CAC_NS, "Country")
            _sub(country, CBC_NS, "IdentificationCode", invoice.buyer.country_code)

    _ubl_payment(root, invoice)

    tax_total = _sub(root, CAC_NS, "TaxTotal")
    money(tax_total, "TaxAmount", invoice.tax_total)
    for group in invoice.tax_groups:
        subtotal = _sub(tax_total, CAC_NS, "TaxSubtotal")
        money(subtotal, "TaxableAmount", group.basis)
        money(subtotal, "TaxAmount", group.tax_amount)
        _ubl_tax_category(subtotal, "TaxCategory", invoice, group.rate, with_reason=True)

    monetary = _sub(root, CAC_NS, "LegalMonetaryTotal")
    money(monetary, "LineExtensionAmount", invoice.net_total)
    money(monetary, "TaxExclusiveAmount", invoice.net_total)
    money(monetary, "TaxInclusiveAmount", invoice.grand_total)
    money(monetary, "PayableAmount", invoice.grand_total)

    for index, line in enumerate(invoice.lines, start=1):
        line_el = _sub(root, CAC_NS, "CreditNoteLine" if credit_note else "InvoiceLine")
        _sub(line_el, CBC_NS, "ID", str(index))
        _sub(
            line_el,
            CBC_NS,
            "CreditedQuantity" if credit_note else "InvoicedQuantity",
            _decimal_text(line.quantity),
            unitCode=line.unit_code,
        )
        money(line_el, "LineExtensionAmount", line.net_amount)
        item = _sub(line_el, CAC_NS, "Item")
        _sub(item, CBC_NS, "Name", line.name)
        _ubl_tax_category(item, "ClassifiedTaxCategory", invoice, line.tax_rate, with_reason=False)
        price = _sub(line_el, CAC_NS, "Price")
        _sub(price, CBC_NS, "PriceAmount", _decimal_text(line.unit_price), currencyID=cur)

    return _to_xml(root)


# --- CII ---------------------------------------------------------------------


def _cii_date(parent: ET.Element, tag: str, value: date) -> None:
    wrapper = _sub(parent, RAM_NS, tag)
    _sub(wrapper, UDT_NS, "DateTimeString", value.strftime("%Y%m%d"), format="102")


def _cii_party(parent: ET.Element, tag: str, party: Party) -> None:
    party_el = _sub(parent, RAM_NS, tag)
    _sub(party_el, RAM_NS, "Name", party.name)
    _sub_if(party_el, RAM_NS, "Description", party.legal_form_text)
    if party.legal_registration_id:
        legal = _sub(party_el, RAM_NS, "SpecifiedLegalOrganization")
        _sub(legal, RAM_NS, "ID", party.legal_registration_id)
    if party.contact_name or party.phone or party.email:
        contact = _sub(party_el, RAM_NS, "DefinedTradeContact")
        _sub_if(contact, RAM_NS, "PersonName", party.contact_name)
        if party.phone:
            phone = _sub(contact, RAM_NS, "TelephoneUniversalCommunication")
            _sub(phone, RAM_NS, "CompleteNumber", party.phone)
        if party.email:
            mail = _sub(contact, RAM_NS, "EmailURIUniversalCommunication")
            _sub(mail, RAM_NS, "URIID", party.email)
    address = _sub(party_el, RAM_NS, "PostalTradeAddress")
    _sub_if(address, RAM_NS, "PostcodeCode", party.postal_code)
    _sub_if(address, RAM_NS, "LineOne", party.street)
    _sub_if(address, RAM_NS, "CityName", party.city)
    _sub(address, RAM_NS, "CountryID", party.country_code)
    if party.email:
        endpoint = _sub(party_el, RAM_NS, "URIUniversalCommunication")
        _sub(endpoint, RAM_NS, "URIID", party.email, schemeID="EM")
    if party.vat_id:
        tax_reg = _sub(party_el, RAM_NS, "SpecifiedTaxRegistration")
        _sub(tax_reg, RAM_NS, "ID", party.vat_id, schemeID="VA")
    if party.tax_number:
        tax_reg = _sub(party_el, RAM_NS, "SpecifiedTaxRegistration")
        _sub(tax_reg, RAM_NS, "ID", party.tax_number, schemeID="FC")


def _cii_line(parent: ET.Element, index: int, line: InvoiceLine, invoice: OutgoingInvoice) -> None:
    item = _sub(parent, RAM_NS, "IncludedSupplyChainTradeLineItem")
    document = _sub(item, RAM_NS, "AssociatedDocumentLineDocument")
    _sub(document, RAM_NS, "LineID", str(index))
    product = _sub(item, RAM_NS, "SpecifiedTradeProduct")
    _sub(product, RAM_NS, "Name", line.name)
    agreement = _sub(item, RAM_NS, "SpecifiedLineTradeAgreement")
    price = _sub(agreement, RAM_NS, "NetPriceProductTradePrice")
    _sub(price, RAM_NS, "ChargeAmount", _decimal_text(line.unit_price))
    delivery = _sub(item, RAM_NS, "SpecifiedLineTradeDelivery")
    _sub(
        delivery,
        RAM_NS,
        "BilledQuantity",
        _decimal_text(line.quantity),
        unitCode=line.unit_code,
    )
    settlement = _sub(item, RAM_NS, "SpecifiedLineTradeSettlement")
    tax = _sub(settlement, RAM_NS, "ApplicableTradeTax")
    _sub(tax, RAM_NS, "TypeCode", "VAT")
    _sub(tax, RAM_NS, "CategoryCode", invoice.category_for(line.tax_rate))
    _sub(tax, RAM_NS, "RateApplicablePercent", _rate(line.tax_rate))
    summation = _sub(settlement, RAM_NS, "SpecifiedTradeSettlementLineMonetarySummation")
    _sub(summation, RAM_NS, "LineTotalAmount", _amount(line.net_amount))


def build_cii(invoice: OutgoingInvoice) -> str:
    root = ET.Element(f"{{{RSM_NS}}}CrossIndustryInvoice")

    context = _sub(root, RSM_NS, "ExchangedDocumentContext")
    process = _sub(context, RAM_NS, "BusinessProcessSpecifiedDocumentContextParameter")
    _sub(process, RAM_NS, "ID", XRECHNUNG_PROFILE_ID)
    guideline = _sub(context, RAM_NS, "GuidelineSpecifiedDocumentContextParameter")
    _sub(guideline, RAM_NS, "ID", XRECHNUNG_CUSTOMIZATION_ID)

    doc = _sub(root, RSM_NS, "ExchangedDocument")
    _sub(doc, RAM_NS, "ID", invoice.invoice_number)
    _sub(doc, RAM_NS, "TypeCode", invoice.type_code)
    issue = _sub(doc, RAM_NS, "IssueDateTime")
    _sub(issue, UDT_NS, "DateTimeString", invoice.issue_date.strftime("%Y%m%d"), format="102")
    for note in invoice.notes:
        if note:
            note_el = _sub(doc, RAM_NS, "IncludedNote")
            _sub(note_el, RAM_NS, "Content", note)

    transaction = _sub(root, RSM_NS, "SupplyChainTradeTransaction")
    for index, line in enumerate(invoice.lines, start=1):
        _cii_line(transaction, index, line, invoice)

    agreement = _sub(transaction, RAM_NS, "ApplicableHeaderTradeAgreement")
    _sub_if(agreement, RAM_NS, "BuyerReference", invoice.buyer_reference)
    _cii_party(agreement, "SellerTradeParty", invoice.seller)
    _cii_party(agreement, "BuyerTradeParty", invoice.buyer)

    delivery = _sub(transaction, RAM_NS, "ApplicableHeaderTradeDelivery")
    if invoice.tax_category == "K":
        # BR-IC-12: Lieferland bei innergemeinschaftlichen Lieferungen.
        ship_to = _sub(delivery, RAM_NS, "ShipToTradeParty")
        address = _sub(ship_to, RAM_NS, "PostalTradeAddress")
        _sub(address, RAM_NS, "CountryID", invoice.buyer.country_code)
    if invoice.service_date is not None:
        event = _sub(delivery, RAM_NS, "ActualDeliverySupplyChainEvent")
        _cii_date(event, "OccurrenceDateTime", invoice.service_date)

    settlement = _sub(transaction, RAM_NS, "ApplicableHeaderTradeSettlement")
    _sub_if(settlement, RAM_NS, "PaymentReference", invoice.payment_reference)
    _sub(settlement, RAM_NS, "InvoiceCurrencyCode", invoice.currency_code)
    if invoice.payment_iban:
        means = _sub(settlement, RAM_NS, "SpecifiedTradeSettlementPaymentMeans")
        _sub(means, RAM_NS, "TypeCode", PAYMENT_MEANS_SEPA_CREDIT_TRANSFER)
        account = _sub(means, RAM_NS, "PayeePartyCreditorFinancialAccount")
        _sub(account, RAM_NS, "IBANID", invoice.payment_iban)
        _sub_if(account, RAM_NS, "AccountName", invoice.payment_account_name)
        if invoice.payment_bic:
            institution = _sub(means, RAM_NS, "PayeeSpecifiedCreditorFinancialInstitution")
            _sub(institution, RAM_NS, "BICID", invoice.payment_bic)
    for group in invoice.tax_groups:
        trade_tax = _sub(settlement, RAM_NS, "ApplicableTradeTax")
        _sub(trade_tax, RAM_NS, "CalculatedAmount", _amount(group.tax_amount))
        _sub(trade_tax, RAM_NS, "TypeCode", "VAT")
        _sub_if(trade_tax, RAM_NS, "ExemptionReason", invoice.exemption_reason)
        _sub(trade_tax, RAM_NS, "BasisAmount", _amount(group.basis))
        _sub(trade_tax, RAM_NS, "CategoryCode", invoice.category_for(group.rate))
        _sub_if(trade_tax, RAM_NS, "ExemptionReasonCode", invoice.exemption_reason_code)
        _sub(trade_tax, RAM_NS, "RateApplicablePercent", _rate(group.rate))
    if invoice.service_period_from is not None or invoice.service_period_to is not None:
        period = _sub(settlement, RAM_NS, "BillingSpecifiedPeriod")
        if invoice.service_period_from is not None:
            _cii_date(period, "StartDateTime", invoice.service_period_from)
        if invoice.service_period_to is not None:
            _cii_date(period, "EndDateTime", invoice.service_period_to)
    if invoice.payment_terms or invoice.due_date is not None:
        terms = _sub(settlement, RAM_NS, "SpecifiedTradePaymentTerms")
        _sub_if(terms, RAM_NS, "Description", invoice.payment_terms)
        if invoice.due_date is not None:
            _cii_date(terms, "DueDateDateTime", invoice.due_date)
    summation = _sub(settlement, RAM_NS, "SpecifiedTradeSettlementHeaderMonetarySummation")
    _sub(summation, RAM_NS, "LineTotalAmount", _amount(invoice.net_total))
    _sub(summation, RAM_NS, "TaxBasisTotalAmount", _amount(invoice.net_total))
    _sub(
        summation,
        RAM_NS,
        "TaxTotalAmount",
        _amount(invoice.tax_total),
        currencyID=invoice.currency_code,
    )
    _sub(summation, RAM_NS, "GrandTotalAmount", _amount(invoice.grand_total))
    _sub(summation, RAM_NS, "DuePayableAmount", _amount(invoice.grand_total))
    if invoice.preceding_invoice_number:
        reference = _sub(settlement, RAM_NS, "InvoiceReferencedDocument")
        _sub(reference, RAM_NS, "IssuerAssignedID", invoice.preceding_invoice_number)
        if invoice.preceding_invoice_date is not None:
            formatted = _sub(reference, RAM_NS, "FormattedIssueDateTime")
            _sub(
                formatted,
                QDT_NS,
                "DateTimeString",
                invoice.preceding_invoice_date.strftime("%Y%m%d"),
                format="102",
            )

    return _to_xml(root)


def build_einvoice(invoice: OutgoingInvoice, *, syntax: str) -> str:
    if syntax == "ubl":
        return build_ubl(invoice)
    if syntax == "cii":
        return build_cii(invoice)
    raise EInvoiceExportError(f"Unbekannte Syntax: {syntax!r} (erwartet 'ubl' oder 'cii').")
