"""Partnererkennung in E-Rechnung, Beleg-OCR und Bank (Sprint 5a, Schritt 5)."""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config
from app.services.bank_import import book_transaction, import_bank_csv, import_bank_statement
from app.services.bank_proposals import (
    book_transaction_to_open_item,
    booking_proposals,
    open_item_suggestions,
    partner_matches,
)
from app.services.bank_rules import BankRuleError, apply_rules, create_rule
from app.services.bank_statement import (
    parse_camt053,
    row_from_mt940_data,
    row_from_open_banking_transaction,
)
from app.services.einvoice_import import EInvoiceParseError, parse_einvoice
from app.services.incoming_invoice import create_invoice_open_item
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.mcp_server import TOOLS
from app.services.partner_identifiers import (
    clean_iban,
    find_ibans,
    find_tax_numbers,
    find_vat_ids,
    leading_iban,
)
from app.services.partner_recognition import (
    STATUS_AMBIGUOUS,
    STATUS_NONE,
    STATUS_UNCERTAIN,
    STATUS_UNIQUE,
    PartnerDirectory,
    PartnerSignals,
    number_hints,
    recognize_partner,
)
from app.services.receipt_ocr import ReceiptExtraction
from app.services.receipt_proposals import propose_receipt_booking
from domain.models import (
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    Account,
    BankTransaction,
    Base,
    BusinessPartner,
    Company,
    Document,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    TaxCode,
    Tenant,
    User,
)

IBAN_A = "DE89370400440532013000"
IBAN_B = "DE02120300000000202051"
IBAN_C = "AT611904300234573201"
VAT_A = "DE123456789"
VAT_B = "DE987654321"
OWN_VAT = "DE111111111"
OWN_IBAN = "DE75512108001245126199"
DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "demo"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _company(session: Session, name: str = "Eigene GmbH", tenant: Tenant | None = None) -> Company:
    tenant = tenant or Tenant(name=f"Mandant {name}")
    company = Company(tenant=tenant, name=name, currency_code="EUR")
    session.add_all([tenant, company])
    session.commit()
    return company


_numbers = iter(range(70001, 79999))


def _partner(session: Session, company: Company, name: str, **fields) -> BusinessPartner:
    if "debtor_number" not in fields and "creditor_number" not in fields:
        fields["creditor_number"] = str(next(_numbers))
    partner = BusinessPartner(
        tenant_id=company.tenant_id, company_id=company.id, name=name, **fields
    )
    session.add(partner)
    session.commit()
    return partner


def _match(session, company, role=None, preferred_role=None, **signals):
    return recognize_partner(
        session,
        company.id,
        PartnerSignals(**signals),
        role=role,
        preferred_role=preferred_role,
    )


# ---------------------------------------------------------------------------
# Kennungen in Fremdtexten
# ---------------------------------------------------------------------------


def test_find_ibans_reads_groups_and_ignores_invalid_checksums():
    text = (
        f"IBAN: DE89 3704 0044 0532 0130 00 BIC COBADEFFXXX, {IBAN_C}, "
        "falsch: DE89370400440532013001, Rechnung DE12 fertig"
    )
    assert find_ibans(text) == [IBAN_A, IBAN_C]
    assert clean_iban("DE89370400440532013001") is None
    assert clean_iban("kein Konto") is None


def test_leading_iban_separates_appended_text():
    assert leading_iban(f"{IBAN_A}BIC+COBADEFF") == IBAN_A
    assert leading_iban(f"{IBAN_A}Muster Lieferant GmbH") == IBAN_A
    assert leading_iban("Muster Lieferant GmbH") is None


def test_find_vat_ids_german_by_pattern_foreign_only_with_label():
    assert find_vat_ids("USt-IdNr.: DE 123 456 789") == [VAT_A]
    assert find_vat_ids("VAT ID: LU26375245") == ["LU26375245"]
    assert find_vat_ids("VAT: IE6388047V") == ["IE6388047V"]
    # Eine IBAN und ein Wort hinter der Bezeichnung sind keine USt-IdNr.
    assert find_vat_ids(f"Konto {IBAN_A}, VAT included") == []
    assert find_vat_ids("Rechnung LU26375245") == []


def test_find_tax_numbers_needs_label():
    assert find_tax_numbers("St.-Nr.: 123/456/78901") == ["12345678901"]
    assert find_tax_numbers("Rechnung 123/456/78901") == []
    # F16: Die Bestellnummer endet zufällig auf „St.-Nr.“.
    assert find_tax_numbers("Best.-Nr. 4500123456") == []
    # N6: Diese Bezeichnungen sind gängig und zählen.
    assert find_tax_numbers("USt-Nr. 123/456/78901") == ["12345678901"]
    assert find_tax_numbers("Umsatzsteuernummer: 123/456/78901") == ["12345678901"]


def test_number_hints_need_label():
    assert number_hints("Rechnung 10001 Kd.-Nr. 10002") == ("10002",)
    assert number_hints("Kundennummer: 10003") == ("10003",)
    assert number_hints("Rechnung 10001") == ()


# ---------------------------------------------------------------------------
# Eindeutig
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("signals", "kind"),
    [
        ({"vat_ids": ("de 123 456 789",)}, "vat_id"),
        ({"ibans": ("DE89 3704 0044 0532 0130 00",)}, "iban"),
        ({"tax_numbers": ("123/456/78901",)}, "tax_number"),
        ({"partner_numbers": ("70500",)}, "partner_number"),
    ],
)
def test_hard_identifier_finds_single_partner(session, signals, kind):
    company = _company(session)
    partner = _partner(
        session,
        company,
        "Muster Lieferant GmbH",
        creditor_number="70500",
        vat_id=VAT_A,
        iban=IBAN_A,
        tax_number="123/456/78901",
    )
    _partner(session, company, "Anderer Lieferant GmbH", vat_id=VAT_B, iban=IBAN_B)

    match = _match(session, company, **signals)

    assert match.status == STATUS_UNIQUE
    assert match.partner.id == partner.id
    assert match.matched_on == (kind,)
    assert not match.needs_confirmation


def test_partner_with_all_identifiers_beats_partner_with_part_of_them(session):
    company = _company(session)
    full = _partner(session, company, "Voll GmbH", vat_id=VAT_A, iban=IBAN_A)
    _partner(session, company, "Nur Konto GmbH", iban=IBAN_A)

    match = _match(session, company, vat_ids=(VAT_A,), ibans=(IBAN_A,))

    assert match.status == STATUS_UNIQUE
    assert match.partner.id == full.id


def test_different_iban_on_unique_partner_warns_but_keeps_partner(session):
    company = _company(session)
    partner = _partner(session, company, "Muster GmbH", vat_id=VAT_A, iban=IBAN_A)

    match = _match(session, company, vat_ids=(VAT_A,), ibans=(IBAN_B,))

    assert match.status == STATUS_UNIQUE
    assert match.partner.id == partner.id
    assert IBAN_B in match.warnings[0] and IBAN_A in match.warnings[0]


# ---------------------------------------------------------------------------
# Unsicher: nie still zuordnen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Muster Lieferant GmbH", "MUSTER LIEFERANT", "Muster Lieferant Deutschland GmbH & Co. KG"],
)
def test_name_alone_is_only_a_suggestion(session, name):
    company = _company(session)
    partner = _partner(session, company, "Muster Lieferant GmbH")

    match = _match(session, company, name=name)

    assert match.status == STATUS_UNCERTAIN
    assert match.partner is None
    assert match.needs_confirmation
    assert [candidate.partner.id for candidate in match.candidates] == [partner.id]
    assert match.as_dict()["partner_id"] is None


def test_umlauts_match_their_transliteration(session):
    company = _company(session)
    partner = _partner(session, company, "Müller Weiß GmbH")

    match = _match(session, company, name="MUELLER WEISS")

    assert [candidate.partner.id for candidate in match.candidates] == [partner.id]


def test_number_in_free_text_is_only_a_suggestion(session):
    company = _company(session)
    partner = _partner(session, company, "Kunde AG", debtor_number="10500")

    match = _match(session, company, number_hints=("10500",))

    assert match.status == STATUS_UNCERTAIN
    assert match.partner is None
    assert match.candidates[0].partner.id == partner.id


def test_iban_with_contradicting_vat_id_is_uncertain(session):
    company = _company(session)
    partner = _partner(session, company, "Muster GmbH", vat_id=VAT_A, iban=IBAN_A)

    match = _match(session, company, vat_ids=(VAT_B,), ibans=(IBAN_A,))

    assert match.status == STATUS_UNCERTAIN
    assert match.partner is None
    assert match.candidates[0].partner.id == partner.id
    assert VAT_B in match.reason


def test_required_role_missing_is_uncertain(session):
    company = _company(session)
    customer = _partner(session, company, "Nur Kunde AG", debtor_number="10001", vat_id=VAT_A)

    match = _match(session, company, role=SUBLEDGER_CREDITOR, vat_ids=(VAT_A,))

    assert match.status == STATUS_UNCERTAIN
    assert match.partner is None
    assert match.candidates[0].partner.id == customer.id
    assert not match.candidates[0].role_fits
    assert "Lieferant" in match.reason


# ---------------------------------------------------------------------------
# Mehrdeutig
# ---------------------------------------------------------------------------


def test_two_partners_with_same_iban_are_ambiguous(session):
    company = _company(session)
    first = _partner(session, company, "Erste GmbH", iban=IBAN_A)
    second = _partner(session, company, "Zweite GmbH", iban=IBAN_A)

    match = _match(session, company, ibans=(IBAN_A,))

    assert match.status == STATUS_AMBIGUOUS
    assert match.partner is None
    assert {candidate.partner.id for candidate in match.candidates} == {first.id, second.id}


def test_vat_id_of_one_and_iban_of_other_partner_are_ambiguous(session):
    """Die USt-IdNr. eines bekannten Lieferanten mit fremder IBAN: Muster einer Fälschung."""
    company = _company(session)
    _partner(session, company, "Echt GmbH", vat_id=VAT_A, iban=IBAN_A)
    _partner(session, company, "Andere GmbH", vat_id=VAT_B, iban=IBAN_B)

    match = _match(session, company, vat_ids=(VAT_A,), ibans=(IBAN_B,))

    assert match.status == STATUS_AMBIGUOUS
    assert match.partner is None
    assert len(match.candidates) == 2


def test_iban_of_one_under_name_of_other_partner_is_ambiguous(session):
    """Der bekannte Absender mit der Bankverbindung eines anderen wird nicht still zugeordnet."""
    company = _company(session)
    owner = _partner(session, company, "Kontoinhaber GmbH", iban=IBAN_A)
    named = _partner(session, company, "Absender GmbH")

    match = _match(session, company, ibans=(IBAN_A,), name="Absender GmbH")

    assert match.status == STATUS_AMBIGUOUS
    assert match.partner is None
    assert [c.partner.id for c in match.candidates] == [owner.id, named.id]
    assert match.candidates[1].matched_on == ("name",)


def test_unknown_name_does_not_contradict_hard_identifier(session):
    """Banken und Belege schreiben Namen anders; nur ein anderer bekannter Partner widerspricht."""
    company = _company(session)
    owner = _partner(session, company, "Kontoinhaber GmbH", iban=IBAN_A)
    _partner(session, company, "Absender GmbH")

    for name in ("Ganz anderer Name", "KONTOINHABER", None):
        match = _match(session, company, ibans=(IBAN_A,), name=name)
        assert match.status == STATUS_UNIQUE
        assert match.partner.id == owner.id


def test_similar_name_of_better_fitting_partner_is_ambiguous(session):
    """F8: Die IBAN der „Müller GmbH“ auf einer Rechnung der „Müller Bau GmbH“."""
    company = _company(session)
    owner = _partner(session, company, "Müller GmbH", iban=IBAN_A)
    better = _partner(session, company, "Müller Bau GmbH")

    match = _match(session, company, ibans=(IBAN_A,), name="Müller Bau GmbH")
    assert match.status == STATUS_AMBIGUOUS
    assert {c.partner.id for c in match.candidates} == {owner.id, better.id}

    # Der eigene Name passt am besten: kein Widerspruch.
    match = _match(session, company, ibans=(IBAN_A,), name="Müller GmbH")
    assert match.status == STATUS_UNIQUE
    assert match.partner.id == owner.id


def test_role_filter_does_not_hide_contradicting_identifier(session):
    """F7: Die Rolle löst Dubletten auf, aber keine widersprüchlichen Kennungen."""
    company = _company(session)
    _partner(session, company, "Nur Kunde AG", debtor_number="10001", vat_id=VAT_A)
    _partner(session, company, "Lieferant GmbH", iban=IBAN_A)

    match = _match(session, company, role=SUBLEDGER_CREDITOR, vat_ids=(VAT_A,), ibans=(IBAN_A,))
    assert match.status == STATUS_AMBIGUOUS
    assert match.partner is None

    other = _company(session, "Zweite GmbH")
    _partner(session, other, "Lieferant GmbH", vat_id=VAT_A)
    _partner(session, other, "Kunde AG", debtor_number="10001", iban=IBAN_A)
    match = _match(session, other, role=SUBLEDGER_CREDITOR, vat_ids=(VAT_A,), ibans=(IBAN_A,))
    assert match.status == STATUS_AMBIGUOUS


def test_name_candidate_with_other_iban_warns(session):
    """F1: Die Abweichung soll sichtbar sein, bevor jemand den Vorschlag bestätigt."""
    company = _company(session)
    _partner(session, company, "Muster GmbH", iban=IBAN_B)

    match = _match(session, company, ibans=(IBAN_A,), name="Muster GmbH")

    assert match.status == STATUS_UNCERTAIN
    assert IBAN_A in match.warnings[0] and IBAN_B in match.warnings[0]


def test_required_role_resolves_duplicate_records(session):
    company = _company(session)
    _partner(session, company, "Firma als Kunde", debtor_number="10001", vat_id=VAT_A)
    supplier = _partner(session, company, "Firma als Lieferant", vat_id=VAT_A)

    match = _match(session, company, role=SUBLEDGER_CREDITOR, vat_ids=(VAT_A,))

    assert match.status == STATUS_UNIQUE
    assert match.partner.id == supplier.id


def test_preferred_role_only_sorts_and_stays_ambiguous(session):
    """Ein Zahlungseingang kann auch die Erstattung eines Lieferanten sein."""
    company = _company(session)
    supplier = _partner(session, company, "Firma als Lieferant", iban=IBAN_A)
    customer = _partner(session, company, "Firma als Kunde", debtor_number="10001", iban=IBAN_A)

    match = _match(session, company, preferred_role=SUBLEDGER_DEBTOR, ibans=(IBAN_A,))

    assert match.status == STATUS_AMBIGUOUS
    assert [c.partner.id for c in match.candidates] == [customer.id, supplier.id]


def test_two_similar_names_are_ambiguous(session):
    company = _company(session)
    _partner(session, company, "Müller GmbH")
    _partner(session, company, "Müller AG")

    match = _match(session, company, name="Müller")

    assert match.status == STATUS_AMBIGUOUS
    assert match.partner is None


# ---------------------------------------------------------------------------
# Kein Treffer
# ---------------------------------------------------------------------------


def test_no_signal_and_unknown_signal_find_nothing(session):
    company = _company(session)
    _partner(session, company, "Muster GmbH", vat_id=VAT_A, iban=IBAN_A)

    assert _match(session, company).status == STATUS_NONE
    assert _match(session, company, vat_ids=(VAT_B,), name="Ganz Anders").status == STATUS_NONE
    # Nur Rechtsform: kein Namensbestandteil, der vergleichbar wäre.
    assert _match(session, company, name="GmbH & Co. KG").status == STATUS_NONE


def test_inactive_partner_is_never_matched_but_mentioned(session):
    company = _company(session)
    _partner(session, company, "Alt GmbH", vat_id=VAT_A, is_active=False)

    match = _match(session, company, vat_ids=(VAT_A,), name="Alt GmbH")

    assert match.status == STATUS_NONE
    assert match.partner is None
    assert "inaktiv" in match.warnings[0]


def test_own_identifiers_of_the_company_do_not_count(session):
    """Auf der Eingangsrechnung steht auch die USt-IdNr. des Empfängers."""
    company = _company(session)
    company.vat_id = OWN_VAT
    company.iban = OWN_IBAN
    session.commit()
    _partner(session, company, "Verbundene GmbH", vat_id=OWN_VAT, iban=OWN_IBAN)

    assert _match(session, company, vat_ids=(OWN_VAT,)).status == STATUS_NONE
    assert _match(session, company, ibans=(OWN_IBAN,)).status == STATUS_NONE


# ---------------------------------------------------------------------------
# Mandantentrennung
# ---------------------------------------------------------------------------


def test_recognition_stays_inside_the_company(session):
    tenant = Tenant(name="Gemeinsamer Mandant")
    company = _company(session, "Eigene GmbH", tenant)
    sister = _company(session, "Schwester GmbH", tenant)
    foreign = _company(session, "Fremde GmbH")
    for other in (sister, foreign):
        _partner(
            session,
            other,
            "Muster Lieferant GmbH",
            vat_id=VAT_A,
            iban=IBAN_A,
            tax_number="123/456/78901",
            creditor_number="70777",
        )

    for signals in (
        {"vat_ids": (VAT_A,)},
        {"ibans": (IBAN_A,)},
        {"tax_numbers": ("123/456/78901",)},
        {"partner_numbers": ("70777",)},
        {"number_hints": ("70777",)},
        {"name": "Muster Lieferant GmbH"},
    ):
        assert _match(session, company, **signals).status == STATUS_NONE


def test_directory_contains_only_the_given_partners(session):
    company = _company(session)
    partner = _partner(session, company, "Muster GmbH", vat_id=VAT_A)
    directory = PartnerDirectory([partner])
    assert directory.match(PartnerSignals(vat_ids=(VAT_A,))).partner.id == partner.id
    assert PartnerDirectory([]).match(PartnerSignals(vat_ids=(VAT_A,))).status == STATUS_NONE


# ---------------------------------------------------------------------------
# E-Rechnung lesen
# ---------------------------------------------------------------------------


def _cii_xml(
    *,
    vat_id: str = VAT_A,
    iban: str = IBAN_A,
    seller: str = "Muster Lieferant GmbH",
    due: str = "20260715",
    due_payable: str = "119.00",
    number: str = "RE-4711",
) -> bytes:
    tax_reg = (
        f'<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{vat_id}</ram:ID>'
        "</ram:SpecifiedTaxRegistration>"
        '<ram:SpecifiedTaxRegistration><ram:ID schemeID="FC">123/456/78901</ram:ID>'
        "</ram:SpecifiedTaxRegistration>"
        if vat_id
        else ""
    )
    means = (
        "<ram:SpecifiedTradeSettlementPaymentMeans><ram:TypeCode>58</ram:TypeCode>"
        f"<ram:PayeePartyCreditorFinancialAccount><ram:IBANID>{iban}</ram:IBANID>"
        "</ram:PayeePartyCreditorFinancialAccount>"
        "<ram:PayeeSpecifiedCreditorFinancialInstitution><ram:BICID>COBADEFFXXX</ram:BICID>"
        "</ram:PayeeSpecifiedCreditorFinancialInstitution>"
        "</ram:SpecifiedTradeSettlementPaymentMeans>"
        if iban
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice
  xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
  xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>{number}</ram:ID><ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime><udt:DateTimeString format="102">20260615</udt:DateTimeString>
    </ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:ID>L-99</ram:ID>
        <ram:Name>{seller}</ram:Name>
        <ram:PostalTradeAddress>
          <ram:PostcodeCode>10115</ram:PostcodeCode><ram:LineOne>Musterweg 1</ram:LineOne>
          <ram:CityName>Berlin</ram:CityName><ram:CountryID>DE</ram:CountryID>
        </ram:PostalTradeAddress>
        {tax_reg}
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty><ram:Name>Eigene GmbH</ram:Name></ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeSettlement>
      {means}
      <ram:SpecifiedTradePaymentTerms>
        <ram:DueDateDateTime><udt:DateTimeString format="102">{due}</udt:DateTimeString>
        </ram:DueDateDateTime>
      </ram:SpecifiedTradePaymentTerms>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>19.00</ram:CalculatedAmount><ram:BasisAmount>100.00</ram:BasisAmount>
        <ram:RateApplicablePercent>19</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>100.00</ram:LineTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">19.00</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>119.00</ram:GrandTotalAmount>
        <ram:DuePayableAmount>{due_payable}</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>""".encode()


def _ubl_xml() -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>UBL-1</cbc:ID><cbc:IssueDate>2026-06-15</cbc:IssueDate>
  <cbc:DueDate>2026-07-20</cbc:DueDate><cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyIdentification><cbc:ID>L-77</cbc:ID></cac:PartyIdentification>
    <cac:PostalAddress>
      <cbc:StreetName>Musterweg 1</cbc:StreetName><cbc:CityName>Berlin</cbc:CityName>
      <cbc:PostalZone>10115</cbc:PostalZone>
      <cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country>
    </cac:PostalAddress>
    <cac:PartyTaxScheme><cbc:CompanyID>{VAT_A}</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyTaxScheme><cbc:CompanyID>123/456/78901</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>FC</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>UBL Lieferant GmbH</cbc:RegistrationName>
    </cac:PartyLegalEntity>
    <cac:Contact><cbc:ElectronicMail>rechnung@example.org</cbc:ElectronicMail></cac:Contact>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode>
    <cac:PayeeFinancialAccount><cbc:ID>{IBAN_A}</cbc:ID>
      <cac:FinancialInstitutionBranch><cbc:ID>COBADEFFXXX</cbc:ID></cac:FinancialInstitutionBranch>
    </cac:PayeeFinancialAccount>
  </cac:PaymentMeans>
  <cac:TaxTotal><cbc:TaxAmount>19.00</cbc:TaxAmount>
    <cac:TaxSubtotal><cbc:TaxableAmount>100.00</cbc:TaxableAmount><cbc:TaxAmount>19.00</cbc:TaxAmount>
      <cac:TaxCategory><cbc:Percent>19</cbc:Percent></cac:TaxCategory></cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount>100.00</cbc:LineExtensionAmount>
    <cbc:TaxInclusiveAmount>119.00</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount>119.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>""".encode()


def test_parse_cii_seller_details():
    invoice = parse_einvoice(_cii_xml())
    assert invoice.seller_vat_id == VAT_A
    assert invoice.seller_tax_number == "123/456/78901"
    assert invoice.seller_identifier == "L-99"
    assert invoice.seller_iban == IBAN_A
    assert invoice.seller_bic == "COBADEFFXXX"
    assert (invoice.seller_street, invoice.seller_postal_code, invoice.seller_city) == (
        "Musterweg 1",
        "10115",
        "Berlin",
    )
    assert invoice.due_date == date(2026, 7, 15)
    assert invoice.amount_due == Decimal("119.00")


def test_parse_ubl_seller_details():
    invoice = parse_einvoice(_ubl_xml())
    assert invoice.seller_name == "UBL Lieferant GmbH"
    assert invoice.seller_vat_id == VAT_A
    assert invoice.seller_tax_number == "123/456/78901"
    assert invoice.seller_iban == IBAN_A
    assert invoice.seller_email == "rechnung@example.org"
    assert invoice.due_date == date(2026, 7, 20)


def test_parse_ignores_buyer_iban_of_direct_debit_and_invalid_values():
    xml = _cii_xml(iban="").replace(
        b"<ram:ApplicableHeaderTradeSettlement>",
        b"<ram:ApplicableHeaderTradeSettlement><ram:SpecifiedTradeSettlementPaymentMeans>"
        b"<ram:TypeCode>59</ram:TypeCode><ram:PayerPartyDebtorFinancialAccount><ram:IBANID>"
        + IBAN_B.encode()
        + b"</ram:IBANID></ram:PayerPartyDebtorFinancialAccount>"
        b"</ram:SpecifiedTradeSettlementPaymentMeans>",
    )
    assert parse_einvoice(xml).seller_iban == ""
    # Falsche Prüfsumme und unlesbare Fälligkeit verhindern den Import nicht.
    invoice = parse_einvoice(_cii_xml(iban="DE89370400440532013001", due="demnaechst"))
    assert invoice.seller_iban == ""
    assert invoice.due_date is None


def test_parse_still_rejects_broken_xml():
    with pytest.raises(EInvoiceParseError):
        parse_einvoice(b"<Invoice><unclosed>")


def test_demo_invoices_without_seller_details_still_parse():
    invoice = parse_einvoice((DEMO_DIR / "erechnung_cii.xml").read_bytes())
    assert invoice.seller_name == "Muster Lieferant GmbH"
    assert invoice.due_date is None or isinstance(invoice.due_date, date)


# ---------------------------------------------------------------------------
# REST und Oberfläche
# ---------------------------------------------------------------------------


@pytest.fixture()
def books(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'recognition.db'}",
            "DOCUMENT_UPLOAD_DIR": str(tmp_path / "docs"),
        }
    )
    return _Books(app)


class _Books:
    """Zwei Mandanten mit je einer Gesellschaft, Konten und Benutzern."""

    def __init__(self, app):
        self.app = app
        self.factory = app.extensions["db_session_factory"]
        with self.factory() as session:
            self.company_id, self.ids = self._setup(session, "Eigene GmbH", "a")
            self.other_company_id, self.other_ids = self._setup(session, "Fremde GmbH", "b")
            tenant_id = session.get(Company, self.company_id).tenant_id
            session.add(
                User(
                    username="admin",
                    password_hash=hash_password("admin123"),
                    role="Admin",
                    tenant_id=None,
                )
            )
            session.add(
                User(
                    username="pruefer-a",
                    password_hash=hash_password("x"),
                    role="Pruefer",
                    tenant_id=tenant_id,
                    api_token_hash=hash_api_token("obk_pruefer-a"),
                    api_token_last4="er-a",
                )
            )
            session.commit()
        self.client = app.test_client()
        self.writer = {"Authorization": "Bearer obk_writer-a"}
        self.reader = {"Authorization": "Bearer obk_pruefer-a"}

    def _setup(self, session, name, suffix):
        tenant = Tenant(name=f"Mandant {name}")
        company = Company(tenant=tenant, name=name, currency_code="EUR")
        session.add_all([tenant, company])
        session.flush()
        ids = {}
        for key, code, label, kind, subledger in (
            ("expense", "4200", "Miete", "expense", None),
            ("vat", "1576", "Vorsteuer 19 %", "asset", None),
            ("creditor", "1600", "Verbindlichkeiten aLuL", "liability", "creditor"),
            ("debtor", "1400", "Forderungen aLuL", "asset", "debtor"),
            ("other_liability", "1700", "Sonstige Verbindlichkeiten", "liability", None),
            ("bank", "1200", "Bank", "asset", None),
        ):
            account = Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=label,
                account_type=kind,
                subledger=subledger,
            )
            session.add(account)
            session.flush()
            ids[key] = account.id
        tax_code = TaxCode(
            tenant_id=tenant.id,
            company_id=company.id,
            code="VSt19",
            kind="input",
            rate=Decimal("19.00"),
            vat_account_id=ids["vat"],
        )
        session.add(tax_code)
        session.flush()
        ids["tax_code"] = tax_code.id
        session.add(
            User(
                username=f"writer-{suffix}",
                password_hash=hash_password("x"),
                role="Buchhalter",
                tenant_id=tenant.id,
                api_token_hash=hash_api_token(f"obk_writer-{suffix}"),
                api_token_last4="last",
            )
        )
        session.commit()
        return company.id, ids

    def partner(self, name="Muster Lieferant GmbH", company_id=None, **fields) -> int:
        company_id = company_id or self.company_id
        with self.factory() as session:
            company = session.get(Company, company_id)
            return _partner(session, company, name, **fields).id

    def import_payload(self, xml=None, **extra) -> dict:
        return {
            "company_id": self.company_id,
            "expense_account_id": self.ids["expense"],
            "creditor_account_id": self.ids["creditor"],
            "tax_code_id": self.ids["tax_code"],
            "file_name": "rechnung.xml",
            "content_base64": base64.b64encode(xml or _cii_xml()).decode("ascii"),
            **extra,
        }

    def import_invoice(self, xml=None, **extra):
        response = self.client.post(
            "/api/v1/einvoices/import",
            json=self.import_payload(xml, **extra),
            headers=self.writer,
        )
        assert response.status_code == 201, response.get_json()
        return response.get_json()

    def entry_partner_ids(self, entry_id: int) -> set[int | None]:
        with self.factory() as session:
            return set(
                session.execute(
                    select(JournalEntryLine.partner_id).where(
                        JournalEntryLine.journal_entry_id == entry_id,
                        JournalEntryLine.account_id == self.ids["creditor"],
                    )
                ).scalars()
            )

    def open_item(self, item_id: int) -> OpenItem:
        with self.factory() as session:
            item = session.get(OpenItem, item_id)
            session.expunge(item)
            return item


def test_import_assigns_unique_partner_and_creates_open_item(books):
    partner_id = books.partner(vat_id=VAT_A, iban=IBAN_A, payment_term_days=30)

    body = books.import_invoice()

    assert body["partner_id"] == partner_id
    assert body["partner_match"]["status"] == STATUS_UNIQUE
    assert body["warnings"] == []
    assert books.entry_partner_ids(body["journal_entry_id"]) == {partner_id}
    item = books.open_item(body["open_item_id"])
    assert item.item_type == "payable"
    assert item.partner_id == partner_id
    assert item.account_id == books.ids["creditor"]
    assert item.journal_entry_id == body["journal_entry_id"]
    assert item.reference == "RE-4711"
    # Die Fälligkeit der Rechnung geht dem Zahlungsziel des Partners vor.
    assert item.due_date == date(2026, 7, 15)
    assert item.open_amount == Decimal("119.00")
    assert item.counterparty_iban == IBAN_A


def test_import_uses_payment_term_when_invoice_has_no_due_date(books):
    partner_id = books.partner(vat_id=VAT_A, payment_term_days=14)
    body = books.import_invoice(_cii_xml(due=""))
    item = books.open_item(body["open_item_id"])
    assert item.partner_id == partner_id
    assert item.due_date == date(2026, 6, 29)


def test_invoice_cannot_change_iban_of_known_partner(books):
    partner_id = books.partner(vat_id=VAT_A, iban=IBAN_B)

    body = books.import_invoice()

    assert body["partner_id"] == partner_id
    assert IBAN_A in body["warnings"][0]
    assert books.open_item(body["open_item_id"]).counterparty_iban == IBAN_B
    with books.factory() as session:
        assert session.get(BusinessPartner, partner_id).iban == IBAN_B


def test_import_books_without_partner_on_name_match(books):
    partner_id = books.partner(iban=IBAN_B)

    body = books.import_invoice(_cii_xml(vat_id="", iban=""))

    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_UNCERTAIN
    assert body["partner_match"]["needs_confirmation"] is True
    assert body["partner_match"]["candidates"][0]["partner_id"] == partner_id
    assert books.entry_partner_ids(body["journal_entry_id"]) == {None}
    item = books.open_item(body["open_item_id"])
    assert item.partner_id is None
    assert item.counterparty == "Muster Lieferant GmbH"


def test_import_books_without_partner_when_ambiguous(books):
    first = books.partner("Erste GmbH", iban=IBAN_A)
    second = books.partner("Zweite GmbH", iban=IBAN_A)

    body = books.import_invoice(_cii_xml(vat_id=""))

    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_AMBIGUOUS
    assert {c["partner_id"] for c in body["partner_match"]["candidates"]} == {first, second}
    assert books.entry_partner_ids(body["journal_entry_id"]) == {None}


def test_import_without_match_keeps_invoice_data_on_open_item(books):
    body = books.import_invoice()

    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_NONE
    item = books.open_item(body["open_item_id"])
    assert item.partner_id is None
    assert item.counterparty == "Muster Lieferant GmbH"
    assert item.counterparty_iban == IBAN_A
    assert item.counterparty_bic == "COBADEFFXXX"


def test_confirmed_partner_wins_over_recognition(books):
    books.partner("Erkannt GmbH", vat_id=VAT_A)
    confirmed = books.partner("Bestätigt GmbH")

    body = books.import_invoice(partner_id=confirmed)

    assert body["partner_id"] == confirmed
    assert books.entry_partner_ids(body["journal_entry_id"]) == {confirmed}


def test_auto_partner_can_be_switched_off(books):
    books.partner(vat_id=VAT_A)
    body = books.import_invoice(auto_partner=False)
    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_UNIQUE
    assert books.entry_partner_ids(body["journal_entry_id"]) == {None}


def test_customer_only_partner_stays_off_the_creditor_account(books):
    customer = books.partner("Nur Kunde AG", debtor_number="10001", vat_id=VAT_A)

    body = books.import_invoice()

    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_UNCERTAIN
    assert body["partner_match"]["candidates"][0]["partner_id"] == customer
    assert books.open_item(body["open_item_id"]).partner_id is None


def test_open_item_only_on_creditor_subledger_and_with_amount_due(books):
    body = books.import_invoice(creditor_account_id=books.ids["other_liability"])
    assert body["open_item_id"] is None
    # Schon bezahlt (BT-115 = 0): kein Posten.
    body = books.import_invoice(_cii_xml(due_payable="0.00", number="R-2"))
    assert body["open_item_id"] is None
    # Anzahlung: Posten nur über den Rest, mit Hinweis.
    body = books.import_invoice(_cii_xml(due_payable="19.00", number="R-3"))
    assert books.open_item(body["open_item_id"]).open_amount == Decimal("19.00")
    assert "unter dem Bruttobetrag" in body["warnings"][0]
    body = books.import_invoice(_cii_xml(number="R-4"), create_open_item=False)
    assert body["open_item_id"] is None


def test_due_amount_above_gross_is_capped_and_reported(books):
    """F2: Der Posten darf nicht mehr nennen als das Hauptbuch."""
    body = books.import_invoice(_cii_xml(due_payable="1190.00"))
    assert books.open_item(body["open_item_id"]).open_amount == Decimal("119.00")
    assert "über dem Bruttobetrag" in body["warnings"][0]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1E+30", "kaputt"])
def test_unusable_due_amount_counts_as_missing(books, value):
    """F13: Solche Werte dürfen den Import nicht abstürzen lassen."""
    assert parse_einvoice(_cii_xml(due_payable=value)).due_payable_amount is None
    body = books.import_invoice(_cii_xml(due_payable=value))
    assert books.open_item(body["open_item_id"]).open_amount == Decimal("119.00")


def test_invoice_iban_stays_off_item_while_partner_is_unconfirmed(books):
    """F1: Sonst stünde unter dem Namen des Partners eine fremde IBAN ungeprüft im Zahllauf."""
    books.partner(iban=IBAN_B)

    body = books.import_invoice(_cii_xml(vat_id=""))

    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_UNCERTAIN
    item = books.open_item(body["open_item_id"])
    assert item.counterparty_iban is None
    assert item.counterparty_bic is None
    text = " ".join(body["warnings"])
    assert IBAN_A in text and IBAN_B in text
    assert "nicht auf den offenen Posten" in text


def test_invoice_iban_stays_off_item_when_ambiguous(books):
    books.partner("Erste GmbH", iban=IBAN_A)
    books.partner("Zweite GmbH", iban=IBAN_A)
    body = books.import_invoice(_cii_xml(vat_id=""))
    assert body["partner_match"]["status"] == STATUS_AMBIGUOUS
    assert books.open_item(body["open_item_id"]).counterparty_iban is None


def test_invoice_iban_stays_off_item_when_unique_partner_is_declined(books):
    books.partner(vat_id=VAT_A, iban=IBAN_C)
    body = books.import_invoice(auto_partner=False)
    assert body["partner_id"] is None
    assert body["partner_match"]["status"] == STATUS_UNIQUE
    assert books.open_item(body["open_item_id"]).counterparty_iban is None


def test_confirmed_partner_without_stored_iban_takes_invoice_iban(books):
    partner_id = books.partner()
    body = books.import_invoice(_cii_xml(vat_id=""), partner_id=partner_id)
    item = books.open_item(body["open_item_id"])
    assert item.partner_id == partner_id
    assert item.counterparty_iban == IBAN_A


def test_same_invoice_cannot_be_booked_twice_until_reversed(books):
    """F3: Der zweite Posten wäre eine doppelte Zahlung im Zahllauf."""
    first = books.import_invoice()

    response = books.client.post(
        "/api/v1/einvoices/import", json=books.import_payload(), headers=books.writer
    )
    assert response.status_code == 422
    assert first["posting_number"] in response.get_json()["error"]
    with books.factory() as session:
        assert len(session.execute(select(OpenItem)).scalars().all()) == 1

    preview = books.client.post(
        "/api/v1/einvoices/preview",
        json={
            "company_id": books.company_id,
            "file_name": "rechnung.xml",
            "content_base64": base64.b64encode(_cii_xml()).decode("ascii"),
        },
        headers=books.writer,
    ).get_json()
    assert first["posting_number"] in preview["warnings"][0]

    _reverse(books, first["journal_entry_id"])
    assert books.import_invoice()["open_item_id"] != first["open_item_id"]


def test_other_invoice_of_same_seller_is_not_a_duplicate(books):
    books.import_invoice()
    assert books.import_invoice(_cii_xml(number="RE-4712"))["posting_number"]


@pytest.mark.parametrize(
    "fields",
    [
        {"seller": "MUSTER LIEFERANT GMBH"},
        {"number": "re-4711"},
        {"seller": "Muster Lieferant GmbH Berlin"},
    ],
)
def test_other_spelling_of_same_invoice_is_a_duplicate(books, fields):
    """N2: Sonst stünde eine Rechnung mehrfach im Zahllauf."""
    books.partner(vat_id=VAT_A)
    books.import_invoice()

    response = books.client.post(
        "/api/v1/einvoices/import",
        json=books.import_payload(_cii_xml(**fields)),
        headers=books.writer,
    )

    assert response.status_code == 422
    with books.factory() as session:
        assert len(session.execute(select(OpenItem)).scalars().all()) == 1


def test_same_number_next_year_is_not_a_duplicate(books):
    """N7: Mancher Nummernkreis beginnt jährlich neu."""
    books.import_invoice()
    next_year = _cii_xml().replace(b"20260615", b"20270615")
    assert books.import_invoice(next_year)["posting_number"]


def test_invoices_without_seller_name_are_not_compared_by_text(books):
    """N7: Ohne Absendernamen sagt die gleiche Nummer nichts."""
    books.import_invoice(_cii_xml(seller="", vat_id=VAT_A))
    assert books.import_invoice(_cii_xml(seller="", vat_id=VAT_B))["posting_number"]


def test_broken_grand_total_is_a_parse_error_not_a_crash(books):
    """N5: Vorschau und Import antworten sauber."""
    xml = _cii_xml().replace(
        b"<ram:GrandTotalAmount>119.00", b"<ram:GrandTotalAmount>NaN"
    )
    with pytest.raises(EInvoiceParseError):
        parse_einvoice(xml)
    response = books.client.post(
        "/api/v1/einvoices/preview",
        json={
            "company_id": books.company_id,
            "file_name": "rechnung.xml",
            "content_base64": base64.b64encode(xml).decode("ascii"),
        },
        headers=books.writer,
    )
    assert response.status_code == 422


def test_negative_due_amount_counts_as_missing(books):
    """N5: Sonst entstünde kein Posten, obwohl das Sammelkonto die Schuld trägt."""
    assert parse_einvoice(_cii_xml(due_payable="-5.00")).due_payable_amount is None
    body = books.import_invoice(_cii_xml(due_payable="-5.00"))
    assert books.open_item(body["open_item_id"]).open_amount == Decimal("119.00")
    assert body["warnings"] == []


def test_reversal_closes_open_item_of_incoming_invoice(books):
    books.partner(vat_id=VAT_A)
    body = books.import_invoice()
    with books.factory() as session:
        reverse_journal_entry(
            session=session,
            journal_entry_id=body["journal_entry_id"],
            reversal_date=date(2026, 6, 20),
            changed_by="tester",
        )
    item = books.open_item(body["open_item_id"])
    assert item.status == "settled"
    assert item.open_amount == Decimal("0.00")


def _pay_invoice(books, body, csv_row: str) -> int:
    tx_id = _api_bank_transaction(books, csv_row)
    with books.factory() as session:
        book_transaction_to_open_item(
            session=session,
            transaction_id=tx_id,
            open_item_id=body["open_item_id"],
            changed_by="tester",
        )
        return session.get(BankTransaction, tx_id).journal_entry_id


def _reverse(books, entry_id: int, day: date = date(2026, 7, 20)) -> None:
    with books.factory() as session:
        reverse_journal_entry(
            session=session, journal_entry_id=entry_id, reversal_date=day, changed_by="tester"
        )


def test_partly_paid_invoice_stays_closed_after_both_reversals(books):
    books.partner(vat_id=VAT_A)
    body = books.import_invoice()
    payment = _pay_invoice(books, body, "01.07.2026;Teilzahlung;Muster;;-40,00")
    assert books.open_item(body["open_item_id"]).open_amount == Decimal("79.00")

    _reverse(books, body["journal_entry_id"])
    item = books.open_item(body["open_item_id"])
    assert item.status == "settled"
    assert item.open_amount == Decimal("0.00")

    _reverse(books, payment)
    item = books.open_item(body["open_item_id"])
    assert item.status == "settled"
    assert item.open_amount == Decimal("0.00")


def test_paid_invoice_stays_closed_when_payment_is_reversed_after_invoice(books):
    books.partner(vat_id=VAT_A)
    body = books.import_invoice()
    payment = _pay_invoice(books, body, "01.07.2026;Zahlung;Muster;;-119,00")

    _reverse(books, body["journal_entry_id"])
    assert books.open_item(body["open_item_id"]).status == "settled"
    _reverse(books, payment)

    item = books.open_item(body["open_item_id"])
    assert item.status == "settled"
    assert item.open_amount == Decimal("0.00")


def test_payment_reversed_before_invoice_reopens_then_closes(books):
    books.partner(vat_id=VAT_A)
    body = books.import_invoice()
    payment = _pay_invoice(books, body, "01.07.2026;Zahlung;Muster;;-119,00")

    _reverse(books, payment)
    item = books.open_item(body["open_item_id"])
    assert item.status == "open"
    assert item.open_amount == Decimal("119.00")

    _reverse(books, body["journal_entry_id"])
    assert books.open_item(body["open_item_id"]).status == "settled"


def test_preview_books_nothing_and_proposes_new_supplier(books):
    response = books.client.post(
        "/api/v1/einvoices/preview",
        json={
            "company_id": books.company_id,
            "file_name": "rechnung.xml",
            "content_base64": base64.b64encode(_cii_xml()).decode("ascii"),
        },
        headers=books.reader,
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["invoice"]["seller_vat_id"] == VAT_A
    assert body["invoice"]["due_date"] == "2026-07-15"
    assert body["partner_match"]["status"] == STATUS_NONE
    proposed = body["proposed_partner"]
    assert proposed["name"] == "Muster Lieferant GmbH"
    assert proposed["vat_id"] == VAT_A
    assert proposed["iban"] == IBAN_A
    assert proposed["street"] == "Musterweg 1"
    with books.factory() as session:
        assert session.execute(select(JournalEntry)).first() is None


def test_preview_with_unique_partner_proposes_nothing(books):
    partner_id = books.partner(vat_id=VAT_A)
    body = books.client.post(
        "/api/v1/einvoices/preview",
        json={
            "company_id": books.company_id,
            "file_name": "rechnung.xml",
            "content_base64": base64.b64encode(_cii_xml()).decode("ascii"),
        },
        headers=books.writer,
    ).get_json()
    assert body["partner_match"]["partner_id"] == partner_id
    assert body["proposed_partner"] is None


def test_foreign_company_is_rejected_everywhere(books):
    foreign_partner = books.partner("Fremd GmbH", company_id=books.other_company_id, vat_id=VAT_A)
    encoded = base64.b64encode(_cii_xml()).decode("ascii")

    # Fremde Gesellschaft: Vorschau und Erkennung antworten nicht.
    assert (
        books.client.post(
            "/api/v1/einvoices/preview",
            json={
                "company_id": books.other_company_id,
                "file_name": "r.xml",
                "content_base64": encoded,
            },
            headers=books.writer,
        ).status_code
        == 404
    )
    assert (
        books.client.post(
            "/api/v1/partners/recognize",
            json={"company_id": books.other_company_id, "vat_id": VAT_A},
            headers=books.writer,
        ).status_code
        == 404
    )
    # Die eigene Gesellschaft sieht den fremden Partner nicht.
    body = books.client.post(
        "/api/v1/partners/recognize",
        json={"company_id": books.company_id, "vat_id": VAT_A, "name": "Fremd GmbH"},
        headers=books.writer,
    ).get_json()
    assert body["status"] == STATUS_NONE
    assert body["candidates"] == []
    # Ein fremder Partner lässt sich auch nicht ausdrücklich zuordnen.
    response = books.client.post(
        "/api/v1/einvoices/import",
        json=books.import_payload(partner_id=foreign_partner),
        headers=books.writer,
    )
    assert response.status_code == 422
    with books.factory() as session:
        assert session.execute(select(JournalEntry)).first() is None


def test_recognize_endpoint_statuses_and_read_role(books):
    partner_id = books.partner(vat_id=VAT_A, iban=IBAN_A, creditor_number="70555")
    url = "/api/v1/partners/recognize"
    common = {"company_id": books.company_id}

    unique = books.client.post(url, json={**common, "iban": IBAN_A}, headers=books.reader)
    assert unique.status_code == 200
    assert unique.get_json()["partner_id"] == partner_id

    number = books.client.post(
        url, json={**common, "partner_number": "70555"}, headers=books.reader
    )
    assert number.get_json()["status"] == STATUS_UNIQUE

    name = books.client.post(
        url, json={**common, "name": "Muster Lieferant", "role": "supplier"}, headers=books.reader
    )
    assert name.get_json()["status"] == STATUS_UNCERTAIN
    assert name.get_json()["partner_id"] is None

    bad = books.client.post(url, json={**common, "role": "chef"}, headers=books.reader)
    assert bad.status_code == 400


def _login(books):
    books.client.post("/auth/login", data={"username": "admin", "password": "admin123"})


def _web_book(books, **extra):
    return books.client.post(
        "/erechnung/buchen",
        data={
            "company_id": str(books.company_id),
            "expense_account_id": str(books.ids["expense"]),
            "creditor_account_id": str(books.ids["creditor"]),
            "tax_code_id": str(books.ids["tax_code"]),
            "einvoice_xml": (BytesIO(_cii_xml(number=uuid4().hex[:10])), "rechnung.xml"),
            **extra,
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _last_entry_id(books) -> int:
    with books.factory() as session:
        return session.execute(select(JournalEntry.id).order_by(JournalEntry.id.desc())).scalar()


def test_web_preview_preselects_only_unique_partner(books):
    _login(books)
    partner_id = books.partner(vat_id=VAT_A)
    response = books.client.post(
        "/erechnung/pruefen",
        data={
            "company_id": str(books.company_id),
            "einvoice_xml": (BytesIO(_cii_xml()), "rechnung.xml"),
        },
        content_type="multipart/form-data",
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Erkannt" in html
    assert f'<option value="{partner_id}" selected' in html
    assert "Lieferant anlegen" not in html


def test_web_preview_never_preselects_uncertain_partner(books):
    _login(books)
    partner_id = books.partner()
    html = books.client.post(
        "/erechnung/pruefen",
        data={
            "company_id": str(books.company_id),
            "einvoice_xml": (BytesIO(_cii_xml()), "rechnung.xml"),
        },
        content_type="multipart/form-data",
    ).get_data(as_text=True)
    assert "Bitte bestätigen" in html
    assert f'data-partner-pick="{partner_id}"' in html
    assert " selected" not in html
    # Vorbelegung für die Anlage steht als Wert in den Feldern, ohne name-Attribut.
    assert f'value="{VAT_A}"' in html
    assert 'name="vat_id"' not in html


def test_web_preview_reports_broken_xml(books):
    _login(books)
    response = books.client.post(
        "/erechnung/pruefen",
        data={
            "company_id": str(books.company_id),
            "einvoice_xml": (BytesIO(b"<Invoice><unclosed>"), "rechnung.xml"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert "nicht gelesen" in response.get_data(as_text=True)


def test_web_book_auto_empty_and_explicit_choice(books):
    _login(books)
    partner_id = books.partner(vat_id=VAT_A)

    # Ohne Prüfung per JavaScript: automatisch, nur Eindeutiges.
    html = _web_book(books, partner_id="auto").get_data(as_text=True)
    assert "Partner: Muster Lieferant GmbH" in html
    assert books.entry_partner_ids(_last_entry_id(books)) == {partner_id}

    # Leer heißt nach der Prüfung: bewusst ohne Partner.
    html = _web_book(books, partner_id="").get_data(as_text=True)
    assert "Ohne Partner" in html
    assert books.entry_partner_ids(_last_entry_id(books)) == {None}

    other = books.partner("Andere GmbH")
    _web_book(books, partner_id=str(other))
    assert books.entry_partner_ids(_last_entry_id(books)) == {other}

    _web_book(books, partner_id="auto", skip_open_item="1")
    with books.factory() as session:
        linked = session.execute(
            select(OpenItem).where(OpenItem.journal_entry_id == _last_entry_id(books))
        ).first()
    assert linked is None


def test_web_book_stores_document_with_partner_flow(books):
    _login(books)
    books.partner(vat_id=VAT_A)
    _web_book(books)
    with books.factory() as session:
        document = session.execute(select(Document)).scalar_one()
        assert document.journal_entry_id == _last_entry_id(books)


def test_quick_create_partner_returns_json_and_stays_in_company(books):
    _login(books)
    response = books.client.post(
        "/partner/schnellanlage",
        data={
            "company_id": str(books.company_id),
            "name": "Neu GmbH",
            "vat_id": VAT_A,
            "iban": IBAN_A,
            "bic": "COBADEFFXXX",
            "city": "",
        },
    )
    assert response.status_code == 201
    body = response.get_json()
    with books.factory() as session:
        partner = session.get(BusinessPartner, body["id"])
        assert partner.company_id == books.company_id
        assert partner.creditor_number == body["number"]
        assert partner.debtor_number is None
        assert partner.iban == IBAN_A

    invalid = books.client.post(
        "/partner/schnellanlage",
        data={"company_id": str(books.company_id), "name": "Neu GmbH", "iban": "falsch"},
    )
    assert invalid.status_code == 422
    assert invalid.get_json()["error"]
    with books.factory() as session:
        partners = session.execute(
            select(BusinessPartner).where(BusinessPartner.company_id == books.company_id)
        ).scalars()
        assert len(partners.all()) == 1


# ---------------------------------------------------------------------------
# E-Rechnung-Export
# ---------------------------------------------------------------------------


def _export(books, **extra):
    return books.client.post(
        "/api/v1/einvoices/export",
        json={
            "company_id": books.company_id,
            "invoice_number": "A-1",
            "issue_date": "2026-06-15",
            "syntax": "cii",
            "lines": [{"name": "Beratung", "quantity": "1", "unit_price": "100", "tax_rate": "19"}],
            **extra,
        },
        headers=books.writer,
    )


def test_export_takes_buyer_from_partner(books):
    partner_id = books.partner(
        "Kunde AG",
        debtor_number="10001",
        street="Kundenweg 2",
        postal_code="20095",
        city="Hamburg",
        vat_id=VAT_B,
    )
    xml = _export(books, buyer_partner_id=partner_id).get_json()["xml"]
    buyer = xml[xml.index("BuyerTradeParty") :]
    assert "Kunde AG" in buyer
    assert "Kundenweg 2" in buyer
    assert "20095" in buyer
    assert VAT_B in buyer


def test_export_form_values_win_and_foreign_partner_is_rejected(books):
    partner_id = books.partner("Kunde AG", debtor_number="10001", city="Hamburg")
    xml = _export(books, buyer_partner_id=partner_id, buyer_city="Bremen").get_json()["xml"]
    assert "Bremen" in xml and "Hamburg" not in xml

    foreign = books.partner("Fremd AG", company_id=books.other_company_id, debtor_number="10002")
    assert _export(books, buyer_partner_id=foreign).status_code == 404
    assert _export(books).status_code == 400


# ---------------------------------------------------------------------------
# Beleg-OCR und Belegabgleich
# ---------------------------------------------------------------------------


def _extraction(text: str = "", **fields) -> ReceiptExtraction:
    return ReceiptExtraction(raw_text=text, **fields)


def test_receipt_recognizes_partner_from_text(session):
    company = _company(session)
    partner = _partner(session, company, "Muster GmbH", vat_id=VAT_A)
    by_vat = propose_receipt_booking(session, company.id, _extraction(f"USt-IdNr. {VAT_A}"))
    assert by_vat.partner.id == partner.id
    assert by_vat.partner_match.status == STATUS_UNIQUE
    assert by_vat.proposed_partner is None

    other = _partner(session, company, "Konto GmbH", iban=IBAN_B)
    by_iban = propose_receipt_booking(
        session, company.id, _extraction("IBAN DE02 1203 0000 0000 2020 51")
    )
    assert by_iban.partner.id == other.id
    assert by_iban.supplier_iban == IBAN_B


def test_receipt_name_match_leaves_partner_empty(session):
    company = _company(session)
    partner = _partner(session, company, "Muster Lieferant GmbH")

    proposal = propose_receipt_booking(
        session, company.id, _extraction(supplier="Muster Lieferant")
    )
    data = proposal.as_dict()

    assert proposal.partner is None
    assert data["partner_id"] is None
    assert data["partner_match"]["status"] == STATUS_UNCERTAIN
    assert data["partner_match"]["candidates"][0]["partner_id"] == partner.id
    assert data["proposed_partner"]["name"] == "Muster Lieferant"


def test_receipt_ignores_own_identifiers_and_keeps_foreign_iban(session):
    company = _company(session)
    company.vat_id = OWN_VAT
    company.iban = OWN_IBAN
    session.commit()
    partner = _partner(session, company, "Verbunden GmbH", vat_id=OWN_VAT)

    proposal = propose_receipt_booking(
        session,
        company.id,
        _extraction(f"Empfänger USt-IdNr. {OWN_VAT}, IBAN {OWN_IBAN}, Konto {IBAN_B}"),
    )

    assert proposal.partner is None
    assert partner.id not in [c.partner.id for c in proposal.partner_match.candidates]
    assert proposal.supplier_iban == IBAN_B


def test_receipt_iban_is_not_proposed_for_unconfirmed_candidate(session):
    """F1: Der Belegweg darf die IBAN genauso wenig mitnehmen."""
    company = _company(session)
    _partner(session, company, "Muster GmbH", iban=IBAN_B)

    proposal = propose_receipt_booking(
        session, company.id, _extraction(f"IBAN {IBAN_A}", supplier="Muster GmbH")
    )

    assert proposal.partner_match.status == STATUS_UNCERTAIN
    assert proposal.supplier_iban is None
    # Für „Lieferant anlegen“ bleibt sie sichtbar, dort entscheidet ein Mensch.
    assert proposal.proposed_partner["iban"] == IBAN_A


def test_open_item_guard_drops_iban_under_known_name(session):
    """F1: Die Prüfung sitzt zentral und greift auch, wenn der Aufrufer die IBAN mitgibt."""
    company, _bank, creditor, _debtor = _bank_accounts(session)
    _partner(session, company, "Muster GmbH", iban=IBAN_B)
    entry = _posted_entry(session, company, creditor)

    def create(counterparty: str, **extra) -> OpenItem | None:
        return create_invoice_open_item(
            session=session,
            company=company,
            entry=entry,
            creditor_account_id=creditor.id,
            amount=Decimal("119.00"),
            reference="B-1",
            counterparty=counterparty,
            iban=IBAN_A,
            changed_by="tester",
            **extra,
        )

    assert create("Muster GmbH").counterparty_iban is None
    assert create("Ganz Unbekannt GmbH").counterparty_iban == IBAN_A
    # F10: Ein Nennbetrag in USD ist kein Betrag in Euro.
    assert create("Ganz Unbekannt GmbH", currency_code="USD") is None
    assert create("Ganz Unbekannt GmbH", currency_code="EUR") is not None
    # N4: Schreibweisen des eigenen Codes und unbrauchbare Angaben verhindern keinen Posten.
    assert create("Ganz Unbekannt GmbH", currency_code=" eur ") is not None
    assert create("Ganz Unbekannt GmbH", currency_code="€") is not None


def test_receipt_with_two_partner_vat_ids_stays_open(session):
    company = _company(session)
    _partner(session, company, "A GmbH", vat_id=VAT_A)
    _partner(session, company, "B GmbH", vat_id=VAT_B)

    proposal = propose_receipt_booking(session, company.id, _extraction(f"{VAT_A} und {VAT_B}"))

    assert proposal.partner is None
    assert proposal.partner_match.status == STATUS_AMBIGUOUS


def test_receipt_stays_inside_company(session):
    company = _company(session)
    foreign = _company(session, "Fremde GmbH")
    _partner(session, foreign, "Muster GmbH", vat_id=VAT_A, iban=IBAN_A)

    proposal = propose_receipt_booking(
        session, company.id, _extraction(f"{VAT_A} {IBAN_A}", supplier="Muster GmbH")
    )

    assert proposal.partner_match.status == STATUS_NONE
    assert proposal.partner is None


def _upload_document(books, file_sha256: str | None = None) -> int:
    with books.factory() as session:
        company = session.get(Company, books.company_id)
        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name="beleg.pdf",
            storage_key=f"/tmp/{uuid4().hex}.pdf",
            mime_type="application/pdf",
            file_sha256=file_sha256 or uuid4().hex * 2,
            file_size_bytes=1024,
            document_date=date(2026, 6, 15),
        )
        session.add(document)
        session.commit()
        return document.id


def _ocr_page(books, lines: list[str]) -> str:
    from tests.test_receipt_matching import _pdf_with_text

    _login(books)
    response = books.client.post(
        "/belege/ocr/vorschlag",
        data={
            "company_id": str(books.company_id),
            "document_date": "2026-06-15",
            "document_file": (BytesIO(_pdf_with_text(lines)), "beleg.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    return response.get_data(as_text=True)


OCR_LINES = [
    "Muster Lieferant GmbH",
    "Rechnung Nr. B-1",
    "Rechnungsdatum: 15.06.2026",
    "Zahlbar bis 01.07.2026",
    "Nettobetrag 100,00 EUR",
    "MwSt 19 % 19,00 EUR",
    "Gesamtbetrag 119,00 EUR",
]


def test_ocr_page_shows_name_match_as_suggestion_and_offers_creation(books):
    partner_id = books.partner()

    html = _ocr_page(books, OCR_LINES)

    assert "Bitte bestätigen" in html
    assert f'data-partner-pick="{partner_id}"' in html
    # Die Auswahl der Gesellschaft im Kopf hat dieselben Werte: nur die Partnerauswahl ansehen.
    start = html.index("data-partner-select")
    select = html[start : html.index("</select>", start)]
    assert " selected" not in select
    assert 'type="hidden" name="partner_id"' not in html
    assert "data-partner-create" in html
    assert 'value="2026-07-01"' in html


def test_ocr_page_preselects_unique_partner(books):
    partner_id = books.partner(vat_id=VAT_A)

    html = _ocr_page(books, [*OCR_LINES, f"USt-IdNr. {VAT_A}"])

    assert "Erkannt" in html
    assert f'<option value="{partner_id}" selected>' in html
    assert "data-partner-create" not in html


def test_ocr_web_booking_creates_open_item_unless_skipped(books):
    partner_id = books.partner(iban=IBAN_B)
    _login(books)

    def book(**extra):
        return books.client.post(
            "/belege/ocr/buchen",
            data={
                "company_id": str(books.company_id),
                "document_id": str(_upload_document(books)),
                "expense_account_id": str(books.ids["expense"]),
                "creditor_account_id": str(books.ids["creditor"]),
                "tax_code_id": str(books.ids["tax_code"]),
                "net_amount": "100.00",
                "tax_amount": "19.00",
                "entry_date": "2026-06-15",
                "partner_id": str(partner_id),
                "invoice_number": "B-1",
                "due_date": "2026-07-01",
                "supplier_iban": IBAN_A,
                **extra,
            },
            follow_redirects=True,
        ).get_data(as_text=True)

    assert "Offener Posten angelegt" in book()
    with books.factory() as session:
        item = session.execute(select(OpenItem)).scalar_one()
        assert item.reference == "B-1"
        assert item.partner_id == partner_id
        assert item.due_date == date(2026, 7, 1)
        assert item.counterparty_iban == IBAN_B

    assert "Offener Posten angelegt" not in book(skip_open_item="1")
    with books.factory() as session:
        assert len(session.execute(select(OpenItem)).scalars().all()) == 1


def test_ocr_book_creates_open_item_with_partner(books):
    partner_id = books.partner(iban=IBAN_B)
    document_id = _upload_document(books)
    response = books.client.post(
        "/api/v1/receipt-ocr/book",
        json={
            "company_id": books.company_id,
            "document_id": document_id,
            "expense_account_id": books.ids["expense"],
            "creditor_account_id": books.ids["creditor"],
            "tax_code_id": books.ids["tax_code"],
            "net_amount": "100.00",
            "tax_amount": "19.00",
            "entry_date": "2026-06-15",
            "partner_id": partner_id,
            "invoice_number": "B-1",
            "due_date": "2026-07-01",
            "supplier_iban": IBAN_A,
        },
        headers=books.writer,
    )
    assert response.status_code == 201, response.get_json()
    item = books.open_item(response.get_json()["open_item_id"])
    assert item.reference == "B-1"
    assert item.partner_id == partner_id
    assert item.due_date == date(2026, 7, 1)
    assert item.open_amount == Decimal("119.00")
    # Die IBAN des Stamms geht der des Belegs vor.
    assert item.counterparty_iban == IBAN_B


def test_ocr_book_rejects_document_already_booked_until_reversed(books):
    """F3: Der zweite Posten wäre eine doppelte Zahlung, die erste Buchung verlöre ihren Beleg."""
    document_id = _upload_document(books)
    payload = {
        "company_id": books.company_id,
        "document_id": document_id,
        "expense_account_id": books.ids["expense"],
        "creditor_account_id": books.ids["creditor"],
        "tax_code_id": books.ids["tax_code"],
        "net_amount": "100.00",
        "tax_amount": "19.00",
        "entry_date": "2026-06-15",
    }
    url = "/api/v1/receipt-ocr/book"
    first = books.client.post(url, json=payload, headers=books.writer)
    assert first.status_code == 201

    second = books.client.post(url, json=payload, headers=books.writer)
    assert second.status_code == 409
    with books.factory() as session:
        assert len(session.execute(select(OpenItem)).scalars().all()) == 1
        assert session.get(Document, document_id).journal_entry_id == (
            first.get_json()["journal_entry_id"]
        )

    _reverse(books, first.get_json()["journal_entry_id"])
    assert books.client.post(url, json=payload, headers=books.writer).status_code == 201


def test_ocr_book_rejects_same_file_uploaded_again_until_reversed(books):
    """N3: Ein erneuter Upload legt einen neuen Datensatz an, die Datei bleibt dieselbe."""
    sha = uuid4().hex * 2
    first_document = _upload_document(books, file_sha256=sha)
    second_document = _upload_document(books, file_sha256=sha)
    payload = {
        "company_id": books.company_id,
        "expense_account_id": books.ids["expense"],
        "creditor_account_id": books.ids["creditor"],
        "tax_code_id": books.ids["tax_code"],
        "net_amount": "100.00",
        "tax_amount": "19.00",
        "entry_date": "2026-06-15",
    }
    url = "/api/v1/receipt-ocr/book"
    first = books.client.post(
        url, json={**payload, "document_id": first_document}, headers=books.writer
    )
    assert first.status_code == 201

    again = {**payload, "document_id": second_document}
    assert books.client.post(url, json=again, headers=books.writer).status_code == 409

    _reverse(books, first.get_json()["journal_entry_id"])
    assert books.client.post(url, json=again, headers=books.writer).status_code == 201


def test_ocr_web_rejects_document_already_booked(books):
    _login(books)
    document_id = _upload_document(books)
    data = {
        "company_id": str(books.company_id),
        "document_id": str(document_id),
        "expense_account_id": str(books.ids["expense"]),
        "creditor_account_id": str(books.ids["creditor"]),
        "tax_code_id": str(books.ids["tax_code"]),
        "net_amount": "100.00",
        "tax_amount": "19.00",
        "entry_date": "2026-06-15",
    }
    assert "verbucht" in books.client.post(
        "/belege/ocr/buchen", data=data, follow_redirects=True
    ).get_data(as_text=True)
    html = books.client.post("/belege/ocr/buchen", data=data, follow_redirects=True).get_data(
        as_text=True
    )
    assert "bereits mit einer Buchung" in html
    with books.factory() as session:
        assert len(session.execute(select(JournalEntry)).scalars().all()) == 1


def test_ocr_book_can_skip_open_item(books):
    document_id = _upload_document(books)
    response = books.client.post(
        "/api/v1/receipt-ocr/book",
        json={
            "company_id": books.company_id,
            "document_id": document_id,
            "expense_account_id": books.ids["expense"],
            "creditor_account_id": books.ids["creditor"],
            "net_amount": "100.00",
            "tax_amount": "0",
            "entry_date": "2026-06-15",
            "create_open_item": False,
        },
        headers=books.writer,
    )
    assert response.status_code == 201
    assert response.get_json()["open_item_id"] is None


# ---------------------------------------------------------------------------
# Bank: Gegen-IBAN aus dem Auszug
# ---------------------------------------------------------------------------


def _camt(*entries: str) -> bytes:
    return (
        '<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
        "<BkToCstmrStmt><Stmt>" + "".join(entries) + "</Stmt></BkToCstmrStmt></Document>"
    ).encode()


def _camt_entry(details: str, *, indicator: str = "DBIT", amount: str = "119.00") -> str:
    return (
        f'<Ntry><Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>{indicator}</CdtDbtInd>'
        "<BookgDt><Dt>2026-07-01</Dt></BookgDt><NtryDtls>"
        + details
        + "</NtryDtls></Ntry>"
    )


def _camt_details(iban: str | None, name: str, *, tag: str = "Cdtr") -> str:
    account = f"<{tag}Acct><Id><IBAN>{iban}</IBAN></Id></{tag}Acct>" if iban else ""
    return (
        f"<TxDtls><RltdPties><{tag}><Nm>{name}</Nm></{tag}>{account}</RltdPties>"
        "<RmtInf><Ustrd>Rechnung RE-4711</Ustrd></RmtInf></TxDtls>"
    )


def test_camt_reads_counterparty_iban_of_the_right_side():
    own_account = f"<CdtrAcct><Id><IBAN>{OWN_IBAN}</IBAN></Id></CdtrAcct>"
    credit = _camt_entry(
        _camt_details(IBAN_A, "Kunde AG", tag="Dbtr").replace(
            "</RltdPties>", f"{own_account}</RltdPties>"
        ),
        indicator="CRDT",
    )
    debit = _camt_entry(_camt_details(IBAN_B, "Lieferant GmbH"))
    rows = parse_camt053(_camt(credit, debit))
    assert rows[0].counterparty_iban == IBAN_A
    assert rows[1].counterparty_iban == IBAN_B


def test_camt_batch_with_different_ibans_has_no_counterparty_iban():
    mixed = _camt_entry(_camt_details(IBAN_A, "A") + _camt_details(IBAN_B, "B"))
    same = _camt_entry(_camt_details(IBAN_A, "A") + _camt_details(IBAN_A, "A"))
    partial = _camt_entry(_camt_details(IBAN_A, "A") + _camt_details(None, "B"))
    invalid = _camt_entry(_camt_details("DE89370400440532013001", "A"))
    rows = parse_camt053(_camt(mixed, same, partial, invalid))
    assert [row.counterparty_iban for row in rows] == [None, IBAN_A, None, None]


def _mt940_data(**fields) -> dict:
    class _Amount:
        amount = Decimal("-119.00")
        currency = "EUR"

    return {"amount": _Amount(), "date": date(2026, 7, 1), "purpose": "RE-4711", **fields}


def test_mt940_reads_iban_from_library_fields():
    glued = row_from_mt940_data(1, _mt940_data(applicant_name=f"{IBAN_A}Muster GmbH"))
    assert glued.counterparty_iban == IBAN_A
    # Der Name bleibt wie bisher: Er gehört zum Dedup-Hash.
    assert glued.counterparty == f"{IBAN_A}Muster GmbH"
    gvc = row_from_mt940_data(1, _mt940_data(gvc_applicant_iban=f"{IBAN_B}BIC+COBADEFF"))
    assert gvc.counterparty_iban == IBAN_B
    none = row_from_mt940_data(1, _mt940_data(applicant_name="Muster GmbH"))
    assert none.counterparty_iban is None


def test_open_banking_reads_account_of_the_right_side():
    row = row_from_open_banking_transaction(
        1,
        {
            "transaction_amount": {"amount": "119.00", "currency": "EUR"},
            "credit_debit_indicator": "DBIT",
            "booking_date": "2026-07-01",
            "creditor": {"name": "Muster GmbH"},
            "creditor_account": {"iban": IBAN_A},
            "debtor_account": {"iban": OWN_IBAN},
        },
    )
    assert row.counterparty_iban == IBAN_A


def _bank_accounts(session) -> tuple[Company, Account, Account, Account]:
    company = _company(session)
    accounts = []
    for code, name, kind, subledger in (
        ("1200", "Bank", "asset", None),
        ("1600", "Verbindlichkeiten aLuL", "liability", "creditor"),
        ("1400", "Forderungen aLuL", "asset", "debtor"),
    ):
        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=kind,
            subledger=subledger,
        )
        session.add(account)
        accounts.append(account)
    session.commit()
    return company, *accounts


def _import_csv(session, company, bank, rows: str, header: str = "") -> None:
    header = header or "Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;IBAN;Betrag"
    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(f"{header}\n{rows}\n"),
        changed_by="tester",
    )
    assert report.error_rows == 0, report.errors


def _transactions(session, company) -> list[BankTransaction]:
    return (
        session.execute(
            select(BankTransaction)
            .where(BankTransaction.company_id == company.id)
            .order_by(BankTransaction.id)
        )
        .scalars()
        .all()
    )


def test_csv_alias_and_invalid_iban(session):
    company, bank, *_ = _bank_accounts(session)
    _import_csv(
        session,
        company,
        bank,
        f"01.07.2026;Rechnung;Muster GmbH;{IBAN_A};-119,00\n"
        "02.07.2026;Rechnung;Muster GmbH;kaputt;-20,00",
    )
    assert [tx.counterparty_iban for tx in _transactions(session, company)] == [IBAN_A, None]


def test_counterparty_iban_is_not_part_of_dedup_hash(session):
    company, bank, *_ = _bank_accounts(session)
    header = "Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag"
    _import_csv(session, company, bank, "01.07.2026;Rechnung;Muster GmbH;-119,00", header)
    report = import_bank_statement(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        file_name="auszug.csv",
        content=(
            "Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;IBAN;Betrag\n"
            f"01.07.2026;Rechnung;Muster GmbH;{IBAN_A};-119,00\n"
        ).encode(),
        changed_by="tester",
    )
    assert report.imported_rows == 0
    assert report.duplicate_rows == 1


# ---------------------------------------------------------------------------
# Bank: Erkennung und Vorschlag
# ---------------------------------------------------------------------------


def test_bank_iban_gives_unique_partner_name_only_a_suggestion(session):
    company, bank, *_ = _bank_accounts(session)
    known = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    named = _partner(session, company, "Name GmbH")
    _import_csv(
        session,
        company,
        bank,
        f"01.07.2026;Rechnung;Irgendwer;{IBAN_A};-119,00\n"
        "02.07.2026;Rechnung;NAME GMBH;;-20,00\n"
        "03.07.2026;Rechnung;Niemand;;-30,00",
    )
    first, second, third = _transactions(session, company)

    matches = partner_matches(session=session, transactions=[first, second, third])

    assert matches[first.id].status == STATUS_UNIQUE
    assert matches[first.id].partner.id == known.id
    assert matches[second.id].status == STATUS_UNCERTAIN
    assert matches[second.id].partner is None
    assert matches[second.id].candidates[0].partner.id == named.id
    assert matches[third.id].status == STATUS_NONE


def test_bank_name_after_glued_iban_still_matches(session):
    company, bank, *_ = _bank_accounts(session)
    partner = _partner(session, company, "Muster Lieferant GmbH")
    _import_csv(
        session, company, bank, f"01.07.2026;Rechnung;{IBAN_A}Muster Lieferant;{IBAN_A};-1,00"
    )
    tx = _transactions(session, company)[0]
    match = partner_matches(session=session, transactions=[tx])[tx.id]
    assert match.status == STATUS_UNCERTAIN
    assert match.candidates[0].partner.id == partner.id


def test_bank_purpose_number_is_only_a_suggestion(session):
    company, bank, *_ = _bank_accounts(session)
    partner = _partner(session, company, "Kunde AG", debtor_number="10500")
    _import_csv(session, company, bank, "01.07.2026;Kd.-Nr. 10500 Rechnung;Fremd;;100,00")
    tx = _transactions(session, company)[0]
    match = partner_matches(session=session, transactions=[tx])[tx.id]
    assert match.status == STATUS_UNCERTAIN
    assert match.candidates[0].partner.id == partner.id


def test_unique_iban_overrides_partner_from_history(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    old = _partner(session, company, "Alt GmbH")
    new = _partner(session, company, "Neu GmbH", iban=IBAN_A)
    _import_csv(session, company, bank, "01.06.2026;Abo;Anbieter;;-10,00")
    book_transaction(
        session=session,
        transaction_id=_transactions(session, company)[0].id,
        contra_account_id=creditor.id,
        partner_id=old.id,
        changed_by="tester",
    )
    _import_csv(
        session,
        company,
        bank,
        f"01.07.2026;Abo;Anbieter;{IBAN_A};-10,00\n02.07.2026;Abo;Anbieter;;-11,00",
    )
    _booked, with_iban, without_iban = _transactions(session, company)

    proposals = booking_proposals(session=session, transactions=[with_iban, without_iban])

    assert proposals[with_iban.id].source == "history"
    assert proposals[with_iban.id].partner_id == new.id
    # Ohne IBAN bleibt die Wahl der letzten Buchung derselben Gegenseite.
    assert proposals[without_iban.id].partner_id == old.id


def _posted_entry(session, company, creditor) -> JournalEntry:
    """Eine Buchung, an die sich ein Posten hängen lässt."""
    expense = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="4200",
        name="Miete",
        account_type="expense",
    )
    session.add(expense)
    session.commit()
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 6, 15),
            description="Eingangsrechnung",
            status="posted",
            changed_by="tester",
            lines=[
                JournalLineInput(
                    account_id=expense.id,
                    debit_amount=Decimal("119.00"),
                    credit_amount=Decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=creditor.id,
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("119.00"),
                ),
            ],
        ),
    )


def _payable(session, company, account, partner, reference="RE-4711", amount="119.00"):
    item = OpenItem(
        tenant_id=company.tenant_id,
        company_id=company.id,
        account_id=account.id,
        partner_id=partner.id if partner else None,
        item_type="payable",
        reference=reference,
        counterparty=partner.name if partner else "Freitext GmbH",
        entry_date=date(2026, 6, 15),
        due_date=date(2026, 7, 15),
        original_amount=Decimal(amount),
        open_amount=Decimal(amount),
        currency_code="EUR",
    )
    session.add(item)
    session.commit()
    return item


def test_open_item_suggested_by_partner_and_amount(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    other = _partner(session, company, "Andere GmbH")
    fitting = _payable(session, company, creditor, partner)
    _payable(session, company, creditor, partner, reference="FALSCH", amount="50.00")
    _payable(session, company, creditor, other, reference="X-1")
    _import_csv(session, company, bank, f"01.07.2026;Zahlung;Konto;{IBAN_A};-119,00")
    tx = _transactions(session, company)[0]

    matches = partner_matches(session=session, transactions=[tx])
    found = open_item_suggestions(session=session, transactions=[tx], matches=matches)

    assert [s.item.id for s in found[tx.id]] == [fitting.id]
    assert found[tx.id][0].matched_on == ("partner", "amount")
    # F4: Wer mit dem Vorschlag normal bucht, ließe den Posten offen; er läuft über den Ausgleich.
    assert tx.id not in booking_proposals(session=session, transactions=[tx])


def test_name_match_suggests_open_item_but_does_not_prefill_it(session):
    """Sonst wäre der unsichere Partner über den Posten doch vorbelegt."""
    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Name GmbH")
    item = _payable(session, company, creditor, partner, reference="X-1")
    _import_csv(session, company, bank, "01.07.2026;Zahlung;Name GmbH;;-119,00")
    tx = _transactions(session, company)[0]
    matches = partner_matches(session=session, transactions=[tx])

    found = open_item_suggestions(session=session, transactions=[tx], matches=matches)

    assert [s.item.id for s in found[tx.id]] == [item.id]
    assert tx.id not in booking_proposals(session=session, transactions=[tx])


def test_reference_must_be_a_whole_word(session):
    """F11: „RE-77“ passt nicht auf „RE-776“."""
    company, bank, creditor, _debtor = _bank_accounts(session)
    item = _payable(session, company, creditor, None, reference="RE-77")
    _import_csv(
        session,
        company,
        bank,
        "01.07.2026;Zahlung RE-776;Jemand;;-119,00\n02.07.2026;Zahlung RE-77.;Jemand;;-119,00",
    )
    wrong, right = _transactions(session, company)
    found = open_item_suggestions(
        session=session,
        transactions=[wrong, right],
        matches=partner_matches(session=session, transactions=[wrong, right]),
    )
    assert wrong.id not in found
    assert found[right.id][0].item.id == item.id


def test_inactive_partner_item_is_not_booked_half(session):
    """F12: Ohne Partner blieben im Nebenbuch zwei gegenläufige Salden stehen."""
    from app.services.bank_import import BankImportError

    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Alt GmbH", iban=IBAN_A, is_active=False)
    item = _payable(session, company, creditor, partner, reference="ALT-1")
    _import_csv(session, company, bank, "01.07.2026;Zahlung ALT-1;Alt;;-119,00")
    tx = _transactions(session, company)[0]

    found = open_item_suggestions(
        session=session,
        transactions=[tx],
        matches=partner_matches(session=session, transactions=[tx]),
    )
    assert found == {}
    with pytest.raises(BankImportError, match="inaktiv"):
        book_transaction_to_open_item(
            session=session, transaction_id=tx.id, open_item_id=item.id, changed_by="tester"
        )
    session.refresh(tx)
    assert tx.status == "open"


def test_foreign_currency_transaction_is_rejected_before_booking(session):
    """F5: Sonst wäre der Umsatz gebucht und der Posten bliebe trotzdem offen."""
    from app.services.bank_import import BankImportError

    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    item = _payable(session, company, creditor, partner)
    _import_csv(session, company, bank, f"01.07.2026;Zahlung;Konto;{IBAN_A};-119,00")
    tx = _transactions(session, company)[0]
    tx.currency_code = "USD"
    session.commit()

    found = open_item_suggestions(
        session=session,
        transactions=[tx],
        matches=partner_matches(session=session, transactions=[tx]),
    )
    assert found == {}
    with pytest.raises(BankImportError, match="USD"):
        book_transaction_to_open_item(
            session=session, transaction_id=tx.id, open_item_id=item.id, changed_by="tester"
        )
    session.refresh(tx)
    assert tx.status == "open"
    assert session.execute(select(JournalEntry)).first() is None


def test_failed_settlement_leaves_nothing_booked(session, monkeypatch):
    """F5: Buchung und Ausgleich laufen in einer Transaktion."""
    from app.services import bank_proposals
    from app.services.open_items import OpenItemError

    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    item = _payable(session, company, creditor, partner)
    _import_csv(session, company, bank, f"01.07.2026;Zahlung;Konto;{IBAN_A};-119,00")
    tx = _transactions(session, company)[0]

    def fail(**_kwargs):
        raise OpenItemError("Der offene Posten ist bereits ausgeglichen.")

    monkeypatch.setattr(bank_proposals, "settle_open_item", fail)
    with pytest.raises(OpenItemError):
        book_transaction_to_open_item(
            session=session, transaction_id=tx.id, open_item_id=item.id, changed_by="tester"
        )
    session.refresh(tx)
    assert tx.status == "open"
    assert tx.journal_entry_id is None
    assert session.execute(select(JournalEntry)).first() is None


def _book_history(session, company, bank, account, partner, counterparty: str) -> None:
    _import_csv(session, company, bank, f"01.06.2026;Abo;{counterparty};;-10,00")
    book_transaction(
        session=session,
        transaction_id=_transactions(session, company)[-1].id,
        contra_account_id=account.id,
        partner_id=partner.id,
        changed_by="tester",
    )


def test_history_partner_contradicted_by_iban_owner_is_cleared(session):
    """F6: IBAN des einen, Name und letzte Buchung des anderen: Der Mensch entscheidet."""
    company, bank, creditor, _debtor = _bank_accounts(session)
    _partner(session, company, "Kontoinhaber GmbH", iban=IBAN_A)
    named = _partner(session, company, "Absender GmbH")
    _book_history(session, company, bank, creditor, named, "Absender GmbH")
    _import_csv(session, company, bank, f"01.07.2026;Abo;Absender GmbH;{IBAN_A};-10,00")
    tx = _transactions(session, company)[-1]

    match = partner_matches(session=session, transactions=[tx])[tx.id]
    proposal = booking_proposals(session=session, transactions=[tx])[tx.id]

    assert match.status == STATUS_AMBIGUOUS
    assert proposal.source == "history"
    assert proposal.partner_id is None


def test_history_partner_stays_when_name_supports_it(session):
    """Ein Mensch hat diese Gegenseite zuletzt diesem Partner zugeordnet, der Name stützt das."""
    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Telekom GmbH")
    _book_history(session, company, bank, creditor, partner, "Telekom GmbH")
    _import_csv(session, company, bank, "01.07.2026;Abo;Telekom GmbH;;-10,00")
    tx = _transactions(session, company)[-1]

    proposal = booking_proposals(session=session, transactions=[tx])[tx.id]

    assert proposal.partner_id == partner.id


def test_history_partner_other_than_name_candidate_is_cleared(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    chosen = _partner(session, company, "Ganz Anders GmbH")
    _partner(session, company, "Telekom GmbH")
    _book_history(session, company, bank, creditor, chosen, "Telekom GmbH")
    _import_csv(session, company, bank, "01.07.2026;Abo;Telekom GmbH;;-10,00")
    tx = _transactions(session, company)[-1]

    assert booking_proposals(session=session, transactions=[tx])[tx.id].partner_id is None


def test_history_on_subledger_gives_way_to_waiting_open_item(books):
    """N1: Normal gebucht bliebe der Posten der nächsten Rechnung offen."""
    partner_id = books.partner(iban=IBAN_C)
    first = books.import_invoice(_cii_xml(vat_id="", iban=IBAN_C, number="RE-1"))
    second = books.import_invoice(_cii_xml(vat_id="", iban=IBAN_C, number="RE-2"))
    assert first["partner_id"] == second["partner_id"] == partner_id
    paid = _api_bank_transaction(books, f"01.07.2026;Zahlung RE-1;Muster;{IBAN_C};-119,00")
    assert (
        books.client.post(
            f"/api/v1/bank-transactions/{paid}/book-open-item",
            json={"open_item_id": first["open_item_id"]},
            headers=books.writer,
        ).status_code
        == 200
    )
    next_tx = _api_bank_transaction(books, f"02.07.2026;Zahlung RE-2;Muster;{IBAN_C};-119,00")

    def listed() -> dict:
        rows = books.client.get(
            f"/api/v1/bank-transactions?company_id={books.company_id}&include_suggestions=1",
            headers=books.reader,
        ).get_json()["transactions"]
        return next(row for row in rows if row["id"] == next_tx)

    row = listed()
    assert row["booking_proposal"] is None
    assert row["open_item_suggestions"][0]["open_item_id"] == second["open_item_id"]

    # Ist kein Posten mehr offen, darf die Historie wieder vorschlagen.
    with books.factory() as session:
        session.get(OpenItem, second["open_item_id"]).status = "settled"
        session.commit()
    row = listed()
    assert row["booking_proposal"]["source"] == "history"
    assert row["booking_proposal"]["partner_id"] == partner_id


def test_history_on_subledger_without_partner_counts_every_open_item(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    _import_csv(session, company, bank, "01.06.2026;Abo;Anbieter;;-10,00")
    book_transaction(
        session=session,
        transaction_id=_transactions(session, company)[0].id,
        contra_account_id=creditor.id,
        changed_by="tester",
    )
    _payable(session, company, creditor, None, reference="X-1", amount="500.00")
    _import_csv(session, company, bank, "01.07.2026;Abo;Anbieter;;-11,00")
    tx = _transactions(session, company)[-1]

    assert tx.id not in booking_proposals(session=session, transactions=[tx])


def test_history_on_normal_account_is_not_affected(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    expense = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="4900",
        name="Sonstiges",
        account_type="expense",
    )
    session.add(expense)
    session.commit()
    partner = _partner(session, company, "Anbieter GmbH")
    _payable(session, company, creditor, partner, reference="X-1")
    _book_history(session, company, bank, expense, partner, "Anbieter GmbH")
    _import_csv(session, company, bank, "01.07.2026;Abo;Anbieter GmbH;;-11,00")
    tx = _transactions(session, company)[-1]

    assert booking_proposals(session=session, transactions=[tx])[tx.id].source == "history"


def test_rule_partner_contradicted_by_iban_is_not_booked(session):
    """F14: Der Regel-Lauf bucht ohne Blick eines Menschen, also nicht bei Widerspruch."""
    company, bank, creditor, _debtor = _bank_accounts(session)
    owner = _partner(session, company, "Kontoinhaber GmbH", iban=IBAN_A)
    other = _partner(session, company, "Regel GmbH")
    create_rule(
        session=session,
        company_id=company.id,
        pattern="Abo",
        contra_account_id=creditor.id,
        partner_id=other.id,
        changed_by="tester",
    )
    _import_csv(session, company, bank, f"01.07.2026;Abo;Jemand;{IBAN_A};-10,00")
    tx = _transactions(session, company)[-1]

    assert booking_proposals(session=session, transactions=[tx])[tx.id].partner_id is None
    report = apply_rules(session=session, company_id=company.id, changed_by="tester")

    assert report.booked == 0
    assert owner.name in report.errors[0]
    session.refresh(tx)
    assert tx.status == "open"


def test_open_item_suggested_by_reference_without_partner(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    item = _payable(session, company, creditor, None)
    _import_csv(session, company, bank, "01.07.2026;Zahlung re-4711 danke;Jemand;;-119,00")
    tx = _transactions(session, company)[0]
    found = open_item_suggestions(
        session=session,
        transactions=[tx],
        matches=partner_matches(session=session, transactions=[tx]),
    )
    assert found[tx.id][0].item.id == item.id
    assert found[tx.id][0].matched_on == ("reference", "amount")


def test_open_item_wrong_direction_amount_or_several_gives_no_proposal(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    _payable(session, company, creditor, partner, reference="A-1")
    _payable(session, company, creditor, partner, reference="A-2")
    _import_csv(
        session,
        company,
        bank,
        f"01.07.2026;Zahlung;Konto;{IBAN_A};-119,00\n"
        f"02.07.2026;Erstattung;Konto;{IBAN_A};119,00\n"
        f"03.07.2026;Zahlung;Konto;{IBAN_A};-1,00",
    )
    several, incoming, other_amount = _transactions(session, company)
    transactions = [several, incoming, other_amount]
    matches = partner_matches(session=session, transactions=transactions)
    found = open_item_suggestions(session=session, transactions=transactions, matches=matches)

    assert len(found[several.id]) == 2
    assert incoming.id not in found
    assert other_amount.id not in found
    # Zwei passende Posten: Der Mensch wählt, es gibt keinen Vorschlag für das Gegenkonto.
    assert several.id not in booking_proposals(session=session, transactions=[several])


def test_open_items_of_other_company_are_not_suggested(session):
    company, bank, _creditor, _debtor = _bank_accounts(session)
    foreign = _company(session, "Fremde GmbH")
    foreign_account = Account(
        tenant_id=foreign.tenant_id,
        company_id=foreign.id,
        code="1600",
        name="Kreditoren",
        account_type="liability",
        subledger="creditor",
    )
    session.add(foreign_account)
    session.commit()
    _payable(session, foreign, foreign_account, None)
    _import_csv(session, company, bank, "01.07.2026;Zahlung RE-4711;Jemand;;-119,00")
    tx = _transactions(session, company)[0]
    assert (
        open_item_suggestions(
            session=session,
            transactions=[tx],
            matches=partner_matches(session=session, transactions=[tx]),
        )
        == {}
    )


def test_foreign_currency_item_is_not_suggested_and_not_booked_blindly(session):
    from app.services.bank_import import BankImportError

    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    item = _payable(session, company, creditor, partner)
    item.foreign_currency_code = "USD"
    item.foreign_open_amount = Decimal("130.00")
    session.commit()
    _import_csv(session, company, bank, f"01.07.2026;Zahlung;Konto;{IBAN_A};-119,00")
    tx = _transactions(session, company)[0]

    matches = partner_matches(session=session, transactions=[tx])
    assert open_item_suggestions(session=session, transactions=[tx], matches=matches) == {}
    with pytest.raises(BankImportError, match="USD"):
        book_transaction_to_open_item(
            session=session, transaction_id=tx.id, open_item_id=item.id, changed_by="tester"
        )
    session.refresh(tx)
    assert tx.status == "open"


def test_book_transaction_to_open_item_books_and_settles(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH", iban=IBAN_A)
    item = _payable(session, company, creditor, partner)
    _import_csv(session, company, bank, f"01.07.2026;Zahlung;Konto;{IBAN_A};-119,00")
    tx = _transactions(session, company)[0]

    settled = book_transaction_to_open_item(
        session=session,
        transaction_id=tx.id,
        open_item_id=item.id,
        changed_by="tester",
    )

    assert settled.status == "settled"
    assert settled.open_amount == Decimal("0.00")
    session.refresh(tx)
    assert tx.status == "booked"
    lines = session.execute(
        select(JournalEntryLine).where(
            JournalEntryLine.journal_entry_id == tx.journal_entry_id,
            JournalEntryLine.account_id == creditor.id,
        )
    ).scalars()
    line = next(iter(lines))
    assert line.debit_amount == Decimal("119.00")
    assert line.partner_id == partner.id
    # Das Storno der Zahlung öffnet den Posten wieder.
    reverse_journal_entry(
        session=session,
        journal_entry_id=tx.journal_entry_id,
        reversal_date=date(2026, 7, 2),
        changed_by="tester",
    )
    session.refresh(item)
    assert item.status == "open"
    assert item.open_amount == Decimal("119.00")


def test_book_transaction_to_open_item_checks_before_booking(session):
    from app.services.bank_import import BankImportError

    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Konto GmbH")
    small = _payable(session, company, creditor, partner, amount="10.00")
    _import_csv(
        session,
        company,
        bank,
        "01.07.2026;Zahlung;Konto;;-119,00\n02.07.2026;Eingang;Konto;;119,00",
    )
    outgoing, incoming = _transactions(session, company)

    with pytest.raises(BankImportError, match="höher"):
        book_transaction_to_open_item(
            session=session,
            transaction_id=outgoing.id,
            open_item_id=small.id,
            changed_by="tester",
        )
    with pytest.raises(BankImportError, match="Zahlungseingänge"):
        book_transaction_to_open_item(
            session=session,
            transaction_id=incoming.id,
            open_item_id=small.id,
            changed_by="tester",
        )
    session.refresh(outgoing)
    assert outgoing.status == "open"
    assert session.execute(select(JournalEntry)).first() is None


def test_rule_sets_partner_when_applied(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    partner = _partner(session, company, "Telekom GmbH")
    rule = create_rule(
        session=session,
        company_id=company.id,
        pattern="Telekom",
        contra_account_id=creditor.id,
        partner_id=partner.id,
        changed_by="tester",
    )
    assert rule.partner_id == partner.id
    _import_csv(session, company, bank, "01.07.2026;Telekom Rechnung;Telekom;;-49,00")
    tx = _transactions(session, company)[0]
    assert booking_proposals(session=session, transactions=[tx])[tx.id].partner_id == partner.id

    report = apply_rules(session=session, company_id=company.id, changed_by="tester")

    assert report.booked == 1
    session.refresh(tx)
    partner_ids = set(
        session.execute(
            select(JournalEntryLine.partner_id).where(
                JournalEntryLine.journal_entry_id == tx.journal_entry_id
            )
        ).scalars()
    )
    assert partner_ids == {partner.id}


def test_rule_rejects_foreign_and_inactive_partner(session):
    company, _bank, creditor, _debtor = _bank_accounts(session)
    foreign = _company(session, "Fremde GmbH")
    other = _partner(session, foreign, "Fremd GmbH")
    inactive = _partner(session, company, "Inaktiv GmbH", is_active=False)
    for partner_id, message in ((other.id, "nicht gefunden"), (inactive.id, "inaktiv")):
        with pytest.raises(BankRuleError, match=message):
            create_rule(
                session=session,
                company_id=company.id,
                pattern=f"Muster {partner_id}",
                contra_account_id=creditor.id,
                partner_id=partner_id,
                changed_by="tester",
            )


def test_rule_with_customer_on_creditor_account_lands_in_report(session):
    company, bank, creditor, _debtor = _bank_accounts(session)
    customer = _partner(session, company, "Kunde AG", debtor_number="10001")
    create_rule(
        session=session,
        company_id=company.id,
        pattern="Kunde",
        contra_account_id=creditor.id,
        partner_id=customer.id,
        changed_by="tester",
    )
    _import_csv(session, company, bank, "01.07.2026;Kunde;Kunde;;-49,00")
    report = apply_rules(session=session, company_id=company.id, changed_by="tester")
    assert report.booked == 0
    assert len(report.errors) == 1


def _api_bank_transaction(books, csv_row: str, company_id=None, ids=None) -> int:
    company_id = company_id or books.company_id
    ids = ids or books.ids
    with books.factory() as session:
        import_bank_csv(
            session=session,
            company_id=company_id,
            bank_account_id=ids["bank"],
            csv_stream=StringIO(
                f"Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;IBAN;Betrag\n{csv_row}\n"
            ),
            changed_by="tester",
        )
        return session.execute(
            select(BankTransaction.id)
            .where(BankTransaction.company_id == company_id)
            .order_by(BankTransaction.id.desc())
        ).scalar()


def test_api_bank_list_shows_match_and_open_items(books):
    partner_id = books.partner(iban=IBAN_A)
    body = books.import_invoice(_cii_xml(vat_id=""))
    assert body["partner_id"] == partner_id
    tx_id = _api_bank_transaction(books, f"01.07.2026;Zahlung;Muster;{IBAN_A};-119,00")

    listed = books.client.get(
        f"/api/v1/bank-transactions?company_id={books.company_id}&include_suggestions=1",
        headers=books.reader,
    ).get_json()

    tx = next(row for row in listed["transactions"] if row["id"] == tx_id)
    assert tx["counterparty_iban"] == IBAN_A
    assert tx["partner_match"]["status"] == STATUS_UNIQUE
    assert tx["partner_match"]["partner_id"] == partner_id
    assert tx["open_item_suggestions"][0]["open_item_id"] == body["open_item_id"]
    assert tx["booking_proposal"] is None

    # Lesen darf der Prüfer, buchen nicht.
    forbidden = books.client.post(
        f"/api/v1/bank-transactions/{tx_id}/book-open-item",
        json={"open_item_id": body["open_item_id"]},
        headers=books.reader,
    )
    assert forbidden.status_code == 403

    booked = books.client.post(
        f"/api/v1/bank-transactions/{tx_id}/book-open-item",
        json={"open_item_id": body["open_item_id"]},
        headers=books.writer,
    )
    assert booked.status_code == 200, booked.get_json()
    assert booked.get_json()["open_item_status"] == "settled"
    assert booked.get_json()["transaction"]["status"] == "booked"


def test_api_book_open_item_rejects_other_company(books):
    body = books.import_invoice()
    foreign_tx = _api_bank_transaction(
        books,
        "01.07.2026;Zahlung;Muster;;-119,00",
        company_id=books.other_company_id,
        ids=books.other_ids,
    )
    own_tx = _api_bank_transaction(books, "01.07.2026;Zahlung;Muster;;-119,00")

    # Fremder Umsatz: nicht sichtbar.
    response = books.client.post(
        f"/api/v1/bank-transactions/{foreign_tx}/book-open-item",
        json={"open_item_id": body["open_item_id"]},
        headers=books.writer,
    )
    assert response.status_code == 404

    # Fremder Posten am eigenen Umsatz: abgewiesen.
    with books.factory() as session:
        foreign_company = session.get(Company, books.other_company_id)
        foreign_item = _payable(
            session, foreign_company, session.get(Account, books.other_ids["creditor"]), None
        )
        foreign_item_id = foreign_item.id
    response = books.client.post(
        f"/api/v1/bank-transactions/{own_tx}/book-open-item",
        json={"open_item_id": foreign_item_id},
        headers=books.writer,
    )
    assert response.status_code == 422
    with books.factory() as session:
        assert session.get(BankTransaction, own_tx).status == "open"


def test_api_rule_with_partner(books):
    partner_id = books.partner()
    foreign = books.partner("Fremd GmbH", company_id=books.other_company_id)
    url = "/api/v1/bank-booking-rules"
    common = {"company_id": books.company_id, "contra_account_id": books.ids["creditor"]}

    created = books.client.post(
        url, json={**common, "pattern": "Muster", "partner_id": partner_id}, headers=books.writer
    )
    assert created.status_code == 201
    assert created.get_json()["partner_id"] == partner_id

    rejected = books.client.post(
        url, json={**common, "pattern": "Fremd", "partner_id": foreign}, headers=books.writer
    )
    assert rejected.status_code == 422


def test_bank_page_preselects_only_unique_partner(books):
    _login(books)
    known = books.partner("Konto GmbH", iban=IBAN_A)
    named = books.partner("Name GmbH")
    _api_bank_transaction(books, f"01.07.2026;Zahlung;Konto;{IBAN_A};-10,00")
    _api_bank_transaction(books, "02.07.2026;Zahlung;Name GmbH;;-20,00")

    html = books.client.get(f"/bank?company_id={books.company_id}").get_data(as_text=True)

    assert IBAN_A in html
    assert f'<option value="{known}" selected>' in html
    assert f'<option value="{named}" selected>' not in html
    assert f'data-partner-pick="{named}"' in html
    assert "Bitte bestätigen" in html


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_roundtrip_and_clears_partner_of_open_suggestions(tmp_path):
    """F9: Offene Altvorschläge tragen einen Partner aus der alten Namenssuche."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    config = _alembic_config(engine)
    command.upgrade(config, "20261103_0064")
    insert = text(
        "INSERT INTO receipt_match_suggestion (tenant_id, company_id, document_id, "
        "suggestion_type, confidence, llm_used, status, currency_code, reverse_charge, "
        "partner_id, created_at) VALUES (1, 1, :document, 'new_booking', 'niedrig', 0, "
        ":status, 'EUR', 0, 7, '2026-11-01 10:00:00')"
    )
    with engine.begin() as connection:
        connection.execute(insert, {"document": 1, "status": "offen"})
        connection.execute(insert, {"document": 2, "status": "freigegeben"})

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text("SELECT status, partner_id FROM receipt_match_suggestion")
            ).all()
        )
    assert rows == {"offen": None, "freigegeben": 7}
    columns = {column["name"] for column in inspect(engine).get_columns("bank_transaction")}
    assert "counterparty_iban" in columns

    command.downgrade(config, "20261103_0064")
    inspector = inspect(engine)
    assert "counterparty_iban" not in {
        column["name"] for column in inspector.get_columns("bank_transaction")
    }
    assert "partner_id" not in {
        column["name"] for column in inspector.get_columns("bank_booking_rule")
    }
    command.upgrade(config, "head")
    assert "partner_id" in {
        column["name"] for column in inspect(engine).get_columns("bank_booking_rule")
    }


# ---------------------------------------------------------------------------
# MCP und Chat
# ---------------------------------------------------------------------------


def test_mcp_tools_exist_with_partner_arguments():
    tools = {tool.name: tool for tool in TOOLS}
    for name in ("recognize_partner", "preview_einvoice", "book_bank_transaction_to_open_item"):
        assert name in tools
    assert {"partner_id", "auto_partner", "create_open_item"} <= set(
        tools["import_einvoice"].input_schema["properties"]
    )
    assert "partner_id" in tools["create_bank_booking_rule"].input_schema["properties"]
    assert "buyer_partner_id" in tools["export_einvoice"].input_schema["properties"]
    for name in ("approve_receipt_match_suggestion", "book_receipt_ocr_suggestion"):
        assert "create_open_item" in tools[name].input_schema["properties"]


def test_chat_groups_and_confirmation():
    from app.services.chat import CHAT_TOOL_GROUPS, chat_tool_requires_confirmation

    _title, names = CHAT_TOOL_GROUPS["partnererkennung"]
    assert set(names) == {
        "recognize_partner",
        "preview_einvoice",
        "book_bank_transaction_to_open_item",
    }
    assert not chat_tool_requires_confirmation("recognize_partner")
    assert not chat_tool_requires_confirmation("preview_einvoice")
    assert chat_tool_requires_confirmation("book_bank_transaction_to_open_item")
