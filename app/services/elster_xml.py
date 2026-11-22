"""Amtlicher ElsterXML-Datensatz (Schnittstelle Version 11) für die UStVA.

Baut aus einer festgehaltenen UStVA und den Firmenstammdaten den Datensatz, den ERiC prüft und
übermittelt (Verfahren ``ElsterAnmeldung``, Datenart ``UStVA``). Aufbau und Feldreihenfolge folgen
dem Schema ``UStVA-<Jahr>.xsd`` aus der ERiC-Dokumentation; das Schema selbst liegt nicht im Repo
(ERiC-Lizenz, ADR-011).

Regeln aus der Jahresdokumentation:
* Bemessungsgrundlagen in vollen Euro (Cent entfallen), Steuerbeträge mit zwei Nachkommastellen.
* Kennzahl 83 ist Pflicht; Kennzahlen mit 0 entfallen (ERiC meldet sonst Nullwert-Hinweise).
* Kennzahl 09 trägt die fünfstellige Hersteller-ID, sobald der Unternehmer angegeben ist.
* Testfälle tragen im TransferHeader den Testmerker ``700000004``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from domain.models import Company, VatReturn

ELSTER_NS = "http://www.elster.de/elsterxml/schema/v11"
USTVA_NS_TEMPLATE = "http://finkonsens.de/elster/elsteranmeldung/ustva/v{year}"
ELSTER_TESTMERKER = "700000004"
DEFAULT_PRODUCT_NAME = "OpenBuchhaltung"

# Jahre, für die ERiC 44 ein UStVA-Prüfmodul mitbringt; neuere Jahre kommen mit dem
# November-Release (Jahresfortschreibung).
USTVA_MIN_YEAR = 2021
USTVA_MAX_YEAR = 2026

# Bemessungsgrundlagen (GeldBetragOhneCent).
USTVA_WHOLE_EURO_KENNZAHLEN = frozenset(
    "21 35 41 42 43 44 45 46 48 49 50 60 73 76 77 81 84 86 87 89 90 91 93 94 95".split()
)
# Steuer- und Vorsteuerbeträge (GeldBetragMitCent).
USTVA_CENT_KENNZAHLEN = frozenset(
    "36 37 39 47 59 61 62 63 64 65 66 67 69 74 80 83 85 96 98".split()
)
# Interne Zwischensummen ohne eigene ELSTER-Kennzahl (die Steuer zu Kz 81/86/89/93 rechnet ELSTER).
USTVA_INTERNAL_ROWS = frozenset({"USt"})

# Feldreihenfolge des Kontexts ``Umsatzsteuervoranmeldung`` laut Schema (xs:sequence).
USTVA_KENNZAHL_ORDER = (
    "10 21 22 26 29 35 36 37 39 41 42 43 44 45 46 47 48 49 50 59 60 61 62 63 64 65 66 67 "
    "69 70 73 74 76 77 80 81 83 84 85 86 87 89 91 93 90 94 95 96 98"
).split()

_HERSTELLER_ID_RE = re.compile(r"^[0-9]{5}$")
_TAX_OFFICE_RE = re.compile(r"^[0-9]{4}$")
_PLZ_RE = re.compile(r"^[0-9]{5}$")
_STREET_HOUSE_RE = re.compile(r"^(?P<street>.*?)\s*(?P<number>\d{1,9})\s*(?P<suffix>[^\d\s].*)?$")
_PERIOD_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
_PERIOD_QUARTER_RE = re.compile(r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$")


_NUTZDATEN_PLACEHOLDER = "@@NUTZDATEN@@"


class ElsterXmlError(ValueError):
    """Der Datensatz lässt sich aus den vorhandenen Angaben nicht bilden."""


@dataclass(frozen=True, slots=True)
class ElsterXmlOptions:
    """Herstellerangaben und Umgebung eines Datensatzes."""

    hersteller_id: str
    test: bool = True
    product_name: str = DEFAULT_PRODUCT_NAME
    product_version: str = "0"


@dataclass(frozen=True, slots=True)
class UstvaPeriod:
    year: int
    zeitraum: str


def ustva_period(period_label: str) -> UstvaPeriod:
    """``JJJJ-MM`` → Zeitraum ``01``–``12``, ``JJJJ-Qn`` → ``41``–``44``."""
    label = period_label.strip()
    if match := _PERIOD_MONTH_RE.match(label):
        return UstvaPeriod(year=int(match["year"]), zeitraum=match["month"])
    if match := _PERIOD_QUARTER_RE.match(label):
        return UstvaPeriod(year=int(match["year"]), zeitraum=str(40 + int(match["quarter"])))
    raise ElsterXmlError(
        f"Zeitraum {period_label} ist für die UStVA nicht übermittelbar (nur Monat oder Quartal)."
    )


def ustva_datenart_version(year: int) -> str:
    """Datenartversion für ``EricBearbeiteVorgang``, z. B. ``UStVA_2026``."""
    return f"UStVA_{year}"


def elster_tax_number(tax_number: str | None, tax_office_number: str | None) -> str:
    """Steuernummer im 13-stelligen ELSTER-Format.

    Aufbau: Bundesfinanzamtsnummer (4) + ``0`` + die letzten acht Ziffern der Steuernummer laut
    Bescheid (Bezirk und Unterscheidungsnummer mit Prüfziffer). Eine bereits 13-stellige Angabe
    wird übernommen und gegen die Bundesfinanzamtsnummer geprüft.
    """
    digits = re.sub(r"\D", "", tax_number or "")
    office = (tax_office_number or "").strip()
    if office and not _TAX_OFFICE_RE.match(office):
        raise ElsterXmlError("Die Bundesfinanzamtsnummer muss vierstellig sein.")
    if not digits:
        raise ElsterXmlError("Für ELSTER fehlt die Steuernummer der Gesellschaft.")
    if len(digits) == 13:
        if digits[4] != "0":
            raise ElsterXmlError("Die 13-stellige ELSTER-Steuernummer hat an Stelle 5 keine 0.")
        if office and digits[:4] != office:
            raise ElsterXmlError(
                "Die ELSTER-Steuernummer passt nicht zur Bundesfinanzamtsnummer."
            )
        return digits
    if len(digits) not in (10, 11):
        raise ElsterXmlError(
            "Die Steuernummer muss 10 oder 11 Ziffern (Bescheid) bzw. 13 Ziffern (ELSTER) haben."
        )
    if not office:
        raise ElsterXmlError(
            "Für ELSTER fehlt die Bundesfinanzamtsnummer der Gesellschaft (Firmendaten)."
        )
    return f"{office}0{digits[-8:]}"


def _decimal(value: object, kennzahl: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ElsterXmlError(f"Kennzahl {kennzahl} hat keinen gültigen Betrag.") from exc


def _format_whole_euro(amount: Decimal) -> str:
    whole = amount.quantize(Decimal("1"), rounding=ROUND_DOWN)
    return str(int(whole))


def _format_cent(amount: Decimal) -> str:
    cents = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if cents == 0:
        return "0.00"
    return f"{cents:.2f}"


def ustva_kennzahl_values(kennzahlen: list[dict]) -> dict[str, str]:
    """ELSTER-Kennzahlen aus dem Snapshot, formatiert und ohne Nullwerte (Kz 83 immer)."""
    values: dict[str, str] = {}
    for row in kennzahlen:
        code = str(row["kennziffer"]).strip()
        if code in USTVA_INTERNAL_ROWS:
            continue
        amount = _decimal(row["amount"], code)
        if code in USTVA_WHOLE_EURO_KENNZAHLEN:
            formatted = _format_whole_euro(amount)
            if formatted in {"0", "-0"}:
                continue
        elif code in USTVA_CENT_KENNZAHLEN:
            formatted = _format_cent(amount)
            if formatted == "0.00" and code != "83":
                continue
        else:
            raise ElsterXmlError(f"Kennzahl {code} ist in der UStVA nicht vorgesehen.")
        values[code] = formatted
    if "83" not in values:
        raise ElsterXmlError("Die UStVA enthält keine Kennzahl 83.")
    return values


def _split_street(street: str | None) -> tuple[str | None, str | None, str | None]:
    if not street:
        return None, None, None
    match = _STREET_HOUSE_RE.match(street.strip())
    if not match or not match["street"]:
        return street.strip(), None, None
    suffix = (match["suffix"] or "").strip() or None
    return match["street"].strip(), match["number"], suffix


def _clip(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _sub(parent: ET.Element, tag: str, text: str | None) -> ET.Element | None:
    if text is None:
        return None
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _tostring_default_namespace(element: ET.Element, namespace: str) -> str:
    """Serialisiert mit ``xmlns="…"`` statt Präfix (``default_namespace`` scheitert an Attributen).

    Alle Tags des Teilbaums liegen im selben Namensraum; Texte sind escaped, ein ``<`` im
    Ergebnis gehört daher immer zu einem Tag.
    """
    xml = ET.tostring(element, encoding="unicode")
    match = re.search(r'xmlns:(ns\d+)="' + re.escape(namespace) + '"', xml)
    if match is None:
        raise ElsterXmlError("Namensraum des Datensatzes fehlt.")
    prefix = match.group(1)
    xml = xml.replace(f"xmlns:{prefix}=", "xmlns=", 1)
    return re.sub(rf"<(/?){prefix}:", r"<\1", xml)


def _transfer_datenlieferant(company: Company) -> str:
    parts = [company.name, company.street or "", company.postal_code or "", company.city or ""]
    return ";".join(" ".join(part.split()) for part in parts).strip(";")


def build_ustva_elster_xml(
    *,
    vat_return: VatReturn,
    company: Company,
    options: ElsterXmlOptions,
    created_on: date | None = None,
) -> str:
    """Erzeugt den ElsterXML-Datensatz (Vorgang ``send-Auth``) einer UStVA."""
    if not _HERSTELLER_ID_RE.match(options.hersteller_id or ""):
        raise ElsterXmlError("Die Hersteller-ID muss fünfstellig sein (ELSTER_HERSTELLER_ID).")
    period = ustva_period(vat_return.period_label)
    if not USTVA_MIN_YEAR <= period.year <= USTVA_MAX_YEAR:
        raise ElsterXmlError(
            f"Für die UStVA {period.year} ist kein ERiC-Prüfmodul eingebunden "
            f"({USTVA_MIN_YEAR}–{USTVA_MAX_YEAR})."
        )
    tax_number = elster_tax_number(company.tax_number, company.tax_office_number)
    kennzahlen = ustva_kennzahl_values(list(vat_return.kennzahlen))
    created_on = created_on or date.today()

    ns = f"{{{ELSTER_NS}}}"
    root = ET.Element(f"{ns}Elster")

    transfer = ET.SubElement(root, f"{ns}TransferHeader", {"version": "11"})
    _sub(transfer, f"{ns}Verfahren", "ElsterAnmeldung")
    _sub(transfer, f"{ns}DatenArt", "UStVA")
    _sub(transfer, f"{ns}Vorgang", "send-Auth")
    if options.test:
        _sub(transfer, f"{ns}Testmerker", ELSTER_TESTMERKER)
    _sub(transfer, f"{ns}HerstellerID", options.hersteller_id)
    _sub(transfer, f"{ns}DatenLieferant", _transfer_datenlieferant(company))
    datei = ET.SubElement(transfer, f"{ns}Datei")
    _sub(datei, f"{ns}Verschluesselung", "CMSEncryptedData")
    _sub(datei, f"{ns}Kompression", "GZIP")

    daten_teil = ET.SubElement(root, f"{ns}DatenTeil")
    block = ET.SubElement(daten_teil, f"{ns}Nutzdatenblock")
    header = ET.SubElement(block, f"{ns}NutzdatenHeader", {"version": "11"})
    _sub(header, f"{ns}NutzdatenTicket", str(vat_return.id))
    _sub(header, f"{ns}Empfaenger", tax_number[:4]).set("id", "F")
    hersteller = ET.SubElement(header, f"{ns}Hersteller")
    _sub(hersteller, f"{ns}ProduktName", _clip(options.product_name, 50))
    _sub(hersteller, f"{ns}ProduktVersion", _clip(options.product_version, 50))
    nutzdaten = ET.SubElement(block, f"{ns}Nutzdaten")
    nutzdaten.text = _NUTZDATEN_PLACEHOLDER

    ustva_ns = USTVA_NS_TEMPLATE.format(year=period.year)
    u = f"{{{ustva_ns}}}"
    anmeldung = ET.Element(f"{u}Anmeldungssteuern", {"version": str(period.year)})
    _sub(anmeldung, f"{u}Erstellungsdatum", created_on.strftime("%Y%m%d"))

    lieferant = ET.SubElement(anmeldung, f"{u}DatenLieferant")
    _sub(lieferant, f"{u}Name", _clip(company.name, 45))
    _sub(lieferant, f"{u}Strasse", _clip(company.street, 30))
    _sub(lieferant, f"{u}PLZ", _clip(company.postal_code, 12))
    _sub(lieferant, f"{u}Ort", _clip(company.city, 30))
    _sub(lieferant, f"{u}Telefon", _clip(company.phone, 20))
    _sub(lieferant, f"{u}Email", _clip(company.email, 70))

    steuerfall = ET.SubElement(anmeldung, f"{u}Steuerfall")
    unternehmer = ET.SubElement(steuerfall, f"{u}Unternehmer")
    street, house_number, house_suffix = _split_street(company.street)
    postal_code = (company.postal_code or "").strip()
    _sub(unternehmer, f"{u}Bezeichnung", _clip(company.name, 50))
    _sub(unternehmer, f"{u}Str", _clip(street, 33))
    _sub(unternehmer, f"{u}Hausnummer", house_number)
    _sub(unternehmer, f"{u}HNrZusatz", _clip(house_suffix, 15))
    _sub(unternehmer, f"{u}Ort", _clip(company.city, 34))
    _sub(unternehmer, f"{u}PLZ", postal_code if _PLZ_RE.match(postal_code) else None)
    _sub(unternehmer, f"{u}Telefon", _clip(company.phone, 20))
    _sub(unternehmer, f"{u}Email", _clip(company.email, 70))

    voranmeldung = ET.SubElement(steuerfall, f"{u}Umsatzsteuervoranmeldung")
    _sub(voranmeldung, f"{u}Jahr", str(period.year))
    _sub(voranmeldung, f"{u}Zeitraum", period.zeitraum)
    _sub(voranmeldung, f"{u}Steuernummer", tax_number)
    _sub(voranmeldung, f"{u}Kz09", options.hersteller_id)
    for code in USTVA_KENNZAHL_ORDER:
        if code in kennzahlen:
            _sub(voranmeldung, f"{u}Kz{code}", kennzahlen[code])

    # ERiC verlangt Standard-Namensräume ohne Präfix; der Nutzdatenteil bringt seinen eigenen mit.
    ET.indent(root)
    ET.indent(anmeldung, space="  ", level=4)
    outer = _tostring_default_namespace(root, ELSTER_NS)
    inner = _tostring_default_namespace(anmeldung, ustva_ns)
    xml = outer.replace(
        _NUTZDATEN_PLACEHOLDER, "\n" + " " * 8 + inner + "\n" + " " * 6, 1
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"
