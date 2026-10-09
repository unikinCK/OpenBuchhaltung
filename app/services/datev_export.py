"""DATEV-Buchungsstapel-Export im EXTF-Format.

Erzeugt eine EXTF-CSV (Format-Kategorie 21 "Buchungsstapel", Formatversion 13),
die von DATEV importiert werden kann. Der Aufbau folgt der DATEV-Formatbeschreibung
(DATEV Developer Portal, DATEV-Format, Header und Buchungsstapel):

    Zeile 1: Kopfzeile (Metadaten zum Stapel, 31 Felder)
    Zeile 2: Spaltenüberschriften der Buchungssätze
    Zeile 3+: Buchungssätze

Was der DATEV-Import verlangt (Prüfung im Oktober 2026):

* Brutto-Prinzip: „Die Buchungssätze müssen nach dem Brutto-Prinzip vorliegen.
  Buchungen mit Umsatzsteuer werden mit dem Brutto-Betrag und gegebenenfalls
  BU-Schlüssel importiert“; Teilbuchungssätze mit den Nettowerten je Konto in
  eigenen Zeilen lassen sich nicht importieren (DATEV-Hilfe Dok.-Nr. 1036228).
* Automatikkonten (Kontenfunktion AM/AV im DATEV-Kontenrahmen 2026, SKR03
  Art.-Nr. 11174, SKR04 Art.-Nr. 11175, etwa Erlöse 19 % USt 8400/4400 oder
  Wareneingang 19 % Vorsteuer SKR03 3400) errechnen die Steuer selbst aus dem
  gebuchten Betrag. Ein Nettobetrag auf einem Automatikkonto plus eigene
  Steuerzeile ergäbe die Steuer ein zweites Mal (aus 1.000 € Erlös würden
  840,34 € Erlös und 159,66 € + 190,00 € Umsatzsteuer). Der Buchungsschlüssel 40
  hebt die Automatik auf und ist nur auf Automatikkonten zulässig;
  Steuerschlüssel sind nur auf Konten ohne Automatik zulässig
  (Steuerschlüssel-Tabelle 2026, Dok.-Nr. 0907048).
* Konto und Gegenkonto sind Muss-Felder (Dok.-Nr. 1003221); Sätze ohne
  Gegenkonto meldet der Import mit REW00223 und übernimmt sie nur als
  fehlerhafte Buchungen zur Nacharbeit (Dok.-Nr. 1045608).

Aufbau des Exports:

1. Steuerzeilen (Steuerkonten der Steuercodes und die Standard-Steuerkonten
   1571/1576/1771/1776 bzw. 1401/1406/3801/3806) werden ihrer Bemessungsgrundlage
   zugeordnet: einer Zeile derselben Seite, deren Bruttobetrag DATEV auf genau
   diese Steuer zurückrechnet (kaufmännisch gerundet). Beide werden zu einem
   Bruttobetrag zusammengefasst.
2. Auf einem Automatikkonto mit passendem Steuersatz trägt der Buchungssatz
   keinen BU-Schlüssel; auf anderen Konten den Steuerschlüssel 101/102
   (Umsatzsteuer 19/7 %) bzw. 401/402 (Vorsteuer 19/7 %), sofern der
   Kontenrahmen dort Steuer dieser Art zulässt (Zusatzfunktion KU/V/M).
3. Geht das nicht auf den Cent auf (abweichende Rundung der Rechnung, anderer
   Steuersatz, Sonderfunktion wie innergemeinschaftlicher Erwerb, Aufteilung auf
   mehrere Gegenkonten), bleiben Netto- und Steuerzeile getrennt; der Satz auf dem
   Automatikkonto trägt dann den BU-Schlüssel 40. Ebenso Automatikkonten ohne
   Steuerzeile (Abschluss-, Umbuchungen, Saldovorträge) – außer steuerfreien
   Automatikkonten, deren Funktion nur die UStVA-Kennzahl bestimmt.
4. Mehrzeilige Buchungen werden in Buchungssätze mit Konto und Gegenkonto
   zerlegt und über das gemeinsame Belegfeld 1 (Buchungsnummer) gruppiert.
   Ein Buchungssatz mit Steuer führt das Sachkonto der Steuer als Gegenkonto
   (Konto des Steuerschlüssels, „BU Gegenkonto“ in DATEV).

Welche Konten Automatikkonten sind, hängt vom Kontenrahmen ab (SKR04 4400 ist
Erlöse 19 % USt, SKR03 4400 frei verfügbar). Der Export erkennt ihn wie die
Kontenrahmen-Prüfung (``detect_company_chart``) und schreibt ihn in das
Kopffeld „Sachkontenrahmen“; die Kontenfunktionen stehen in
``data/kontenrahmen/datev_kontenfunktionen.csv`` (erzeugt mit
``tools/datev_kontenfunktionen.py``).

Der Export ist bewusst als "DATEV-kompatibler" Stapel ausgelegt (nicht
zertifiziert).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.account_chart_check import detect_company_chart
from app.services.tax_codes import DEFAULT_TAX_CODES
from domain.models import (
    TAX_KIND_INPUT,
    TAX_KIND_OUTPUT,
    Account,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
)

EXTF_FORMAT_NAME = "Buchungsstapel"
EXTF_FORMAT_CATEGORY = 21
EXTF_FORMAT_VERSION = 13
EXTF_VERSION = 700

DATA_COLUMNS = (
    "Umsatz (ohne Soll/Haben-Kz)",
    "Soll/Haben-Kennzeichen",
    "WKZ Umsatz",
    "Kurs",
    "Basis-Umsatz",
    "WKZ Basis-Umsatz",
    "Konto",
    "Gegenkonto (ohne BU-Schlüssel)",
    "BU-Schlüssel",
    "Belegdatum",
    "Belegfeld 1",
    "Belegfeld 2",
    "Skonto",
    "Buchungstext",
)

ACCOUNT_FUNCTIONS_CSV = (
    Path(__file__).resolve().parents[2] / "data" / "kontenrahmen" / "datev_kontenfunktionen.csv"
)
# Kopffeld 27 "Sachkontenrahmen".
CHART_HEADER_CODES = {"skr03": "03", "skr04": "04"}

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

# Buchungsschlüssel 40: Aufhebung der Automatik (nur auf Automatikkonten zulässig).
BU_SUSPEND_AUTOMATIC = "40"
# Dreistellige Steuerschlüssel (DATEV-Empfehlung, gleichwertig zu 3/2/9/8).
TAX_KEYS: dict[tuple[str, Decimal], str] = {
    (TAX_KIND_OUTPUT, Decimal("19")): "101",
    (TAX_KIND_OUTPUT, Decimal("7")): "102",
    (TAX_KIND_INPUT, Decimal("19")): "401",
    (TAX_KIND_INPUT, Decimal("7")): "402",
}
# Standard-Steuerkonten (USt/VSt 19 % und 7 %) beider Kontenrahmen; erkennt
# Steuerzeilen auch ohne Steuercode (E-Rechnung, Belegabgleich, manuelle Zeilen).
STANDARD_TAX_ACCOUNTS: dict[str, tuple[str, Decimal]] = {
    code: (default.kind, default.rate)
    for default in DEFAULT_TAX_CODES
    if default.rate > ZERO
    for code in default.vat_account_codes
}


@dataclass(slots=True)
class DatevExportOptions:
    consultant_number: int = 1000  # Beraternummer
    client_number: int = 1  # Mandantennummer
    account_length: int = 4  # Sachkontennummernlänge
    description: str = "OpenBuchhaltung Buchungsstapel"


@dataclass(frozen=True, slots=True)
class DatevAccountFunction:
    """Steuerfunktionen eines Sachkontos laut DATEV-Kontenrahmen."""

    # Hauptfunktion "AM" (Umsatzsteuer-) bzw. "AV" (Vorsteuer-Automatik) oder None.
    automatic: str | None = None
    # Was die Automatik rechnet: Steuersatz ("19"), "frei" oder "sonder".
    tax: str | None = None
    # Zusatzfunktion der Kontenklasse: "KU" (keine Steuer), "V", "M" oder None.
    restriction: str | None = None

    @property
    def computes_tax(self) -> bool:
        """Bucht DATEV auf diesem Konto selbst Steuer (BU 40 hebt das auf)?"""
        return self.automatic is not None and self.tax != "frei"

    def automatic_rate(self, kind: str) -> Decimal | None:
        """Steuersatz der Automatik, wenn sie Steuer der Art ``kind`` rechnet."""
        expected = TAX_KIND_OUTPUT if self.automatic == "AM" else TAX_KIND_INPUT
        if self.automatic is None or kind != expected or self.tax in (None, "frei", "sonder"):
            return None
        return Decimal(self.tax)

    def allows_tax_key(self, kind: str) -> bool:
        """Darf ein Steuerschlüssel der Art ``kind`` auf diesem Konto stehen?"""
        if self.automatic is not None or self.restriction == "KU":
            return False
        if self.restriction == "V":
            return kind == TAX_KIND_INPUT
        if self.restriction == "M":
            return kind == TAX_KIND_OUTPUT
        return True


@lru_cache(maxsize=1)
def _account_function_table() -> dict[str, tuple[tuple[int, int, str, str], ...]]:
    table: dict[str, list[tuple[int, int, str, str]]] = {}
    with ACCOUNT_FUNCTIONS_CSV.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            table.setdefault(row["kontenrahmen"], []).append(
                (int(row["von"]), int(row["bis"]), row["funktion"], row["steuer"])
            )
    return {chart: tuple(rows) for chart, rows in table.items()}


@lru_cache(maxsize=4096)
def datev_account_function(chart: str | None, code: str) -> DatevAccountFunction:
    """Kontenfunktion einer Sachkontonummer im SKR03/SKR04 (leer, wenn unbekannt)."""
    if chart is None or len(code) != 4 or not (code.isascii() and code.isdigit()):
        return DatevAccountFunction()
    number = int(code)
    automatic = tax = restriction = None
    for start, end, function, tax_info in _account_function_table().get(chart, ()):
        if not start <= number <= end:
            continue
        if function in ("AM", "AV"):
            automatic, tax = function, tax_info
        else:
            restriction = function
    return DatevAccountFunction(automatic=automatic, tax=tax, restriction=restriction)


def datev_tax_from_gross(gross: Decimal, rate: Decimal) -> Decimal:
    """Steuer, die DATEV aus einem Bruttobetrag herausrechnet (kaufmännisch gerundet)."""
    return (gross * rate / (HUNDRED + rate)).quantize(CENT, rounding=ROUND_HALF_UP)


def _fmt_amount(value: Decimal) -> str:
    """DATEV-Betrag: zwei Nachkommastellen, Komma als Dezimaltrenner, ohne Vorzeichen."""
    return f"{abs(value):.2f}".replace(".", ",")


def _fmt_beleg_date(value: date) -> str:
    """Belegdatum im Format TTMM (das Jahr ergibt sich aus dem Wirtschaftsjahr)."""
    return value.strftime("%d%m")


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _clip(value: str | None, length: int) -> str:
    # Steuerzeichen (Zeilenumbrüche) sind in DATEV-Textfeldern unzulässig.
    return " ".join((value or "").split())[:length]


def _header_row(
    *,
    options: DatevExportOptions,
    generated_at: datetime,
    fiscal_year_start: date,
    date_from: date | None,
    date_to: date | None,
    finalized: bool,
    chart: str | None,
) -> str:
    fields = [
        _quote("EXTF"),
        str(EXTF_VERSION),
        str(EXTF_FORMAT_CATEGORY),
        _quote(EXTF_FORMAT_NAME),
        str(EXTF_FORMAT_VERSION),
        generated_at.strftime("%Y%m%d%H%M%S000"),
        "",  # importiert
        _quote("OB"),  # Herkunft
        _quote(""),  # exportiert von
        _quote(""),  # importiert von
        str(options.consultant_number),
        str(options.client_number),
        fiscal_year_start.strftime("%Y%m%d"),
        str(options.account_length),
        date_from.strftime("%Y%m%d") if date_from else "",
        date_to.strftime("%Y%m%d") if date_to else "",
        _quote(_clip(options.description, 30)),
        _quote(""),  # Diktatkürzel
        "1",  # Buchungstyp: 1 = Finanzbuchführung
        "0",  # Rechnungslegungszweck
        # Festschreibung: 1, wenn alle Buchungen des Stapels festgeschrieben sind.
        "1" if finalized else "0",
        _quote("EUR"),
        "",  # reserviert
        _quote(""),  # Derivatskennzeichen
        "",  # reserviert
        "",  # reserviert
        # Sachkontenrahmen, nach dem Automatikkonten und Steuerschlüssel gesetzt sind.
        _quote(CHART_HEADER_CODES.get(chart or "", "")),
        "",  # ID der Branchenlösung
        "",  # reserviert
        _quote(""),  # reserviert
        _quote(""),  # Anwendungsinformation
    ]
    return ";".join(fields)


@dataclass(slots=True, eq=False)
class _TaxGroup:
    """Steuerzeile und die Bemessungsgrundlage(n), in deren Brutto sie aufgeht."""

    tax_line: Any
    rate: Decimal
    kind: str
    # (Zeile der Bemessungsgrundlage, darauf entfallende Steuer)
    bases: list[tuple[Any, Decimal]] = field(default_factory=list)


@dataclass(slots=True, eq=False)
class _Position:
    """Betrag einer Buchung auf einem Konto, bei Steuer als Bruttobetrag.

    Vergleich über die Identität: Zwei gleiche Zeilen bleiben zwei Positionen.
    """

    code: str
    is_debit: bool
    amount: Decimal
    text: str
    function: DatevAccountFunction
    # Nur Bruttopositionen: Steuergruppe, enthaltene Steuer und BU-Schlüssel
    # des Satzes ("" = das Automatikkonto rechnet die Steuer selbst).
    group: _TaxGroup | None = None
    tax: Decimal = ZERO
    tax_key: str = ""

    @property
    def is_gross(self) -> bool:
        return self.group is not None


@dataclass(slots=True)
class _Booking:
    """Ein DATEV-Buchungssatz: Soll- und Habenposition mit Betrag."""

    debit: _Position
    credit: _Position
    amount: Decimal


def _line_amount(line) -> Decimal:
    return line.debit_amount if line.debit_amount > ZERO else line.credit_amount


def _is_debit(line) -> bool:
    return line.debit_amount > ZERO


def _tax_of_line(line, tax_codes: dict[int, TaxCode], tax_accounts: dict[int, tuple]):
    """(Art, Satz) einer Steuerzeile oder None für andere Zeilen."""
    tax_code = tax_codes.get(line.tax_code_id)
    if (
        tax_code is not None
        and tax_code.vat_account_id == line.account_id
        and tax_code.rate > ZERO
    ):
        return tax_code.kind, tax_code.rate
    return tax_accounts.get(line.account_id) or STANDARD_TAX_ACCOUNTS.get(line.code)


def _tax_groups(lines, tax_codes: dict[int, TaxCode], tax_accounts) -> list[_TaxGroup]:
    """Ordnet die Steuerzeilen einer Buchung ihren Bemessungsgrundlagen zu.

    Passt eine einzelne Zeile derselben Seite (bei gleichem Steuercode
    bevorzugt), deren Brutto DATEV auf genau diese Steuer zurückrechnet, gewinnt
    die nächstgelegene davor. Sonst darf sich die Steuerzeile auf mehrere
    Grundlagen verteilen, wenn deren einzeln gerundete Steuern sie genau ergeben.
    Was nicht aufgeht, bleibt eigene Steuerzeile.
    """
    taxes = {id(line): _tax_of_line(line, tax_codes, tax_accounts) for line in lines}
    paired: set[int] = set()
    groups: list[_TaxGroup] = []
    for tax_line in lines:
        tax_info = taxes[id(tax_line)]
        if tax_info is None:
            continue
        kind, rate = tax_info
        tax_amount = _line_amount(tax_line)
        candidates = [
            line
            for line in lines
            if taxes[id(line)] is None
            and id(line) not in paired
            and _is_debit(line) == _is_debit(tax_line)
        ]
        same_code = [
            line
            for line in candidates
            if tax_line.tax_code_id is not None and line.tax_code_id == tax_line.tax_code_id
        ]
        if same_code:
            candidates = same_code
        else:
            candidates = [
                line
                for line in candidates
                if line.tax_code_id is None
                or (
                    line.tax_code_id in tax_codes
                    and (tax_codes[line.tax_code_id].kind, tax_codes[line.tax_code_id].rate)
                    == (kind, rate)
                )
            ]
        group = _TaxGroup(tax_line=tax_line, rate=rate, kind=kind)
        singles = [
            line
            for line in candidates
            if datev_tax_from_gross(_line_amount(line) + tax_amount, rate) == tax_amount
        ]
        if singles:
            before = [line for line in singles if line.line_number < tax_line.line_number]
            base = (
                max(before, key=lambda line: line.line_number)
                if before
                else min(singles, key=lambda line: line.line_number)
            )
            group.bases.append((base, tax_amount))
        elif len(candidates) > 1:
            shares = [
                (
                    line,
                    (_line_amount(line) * rate / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP),
                )
                for line in candidates
            ]
            if sum(share for _, share in shares) == tax_amount and all(
                share > ZERO
                and datev_tax_from_gross(_line_amount(line) + share, rate) == share
                for line, share in shares
            ):
                group.bases.extend(shares)
        if group.bases:
            paired.update(id(base) for base, _ in group.bases)
            groups.append(group)
    return groups


def _gross_tax_key(function: DatevAccountFunction, kind: str, rate: Decimal) -> str | None:
    """BU-Schlüssel für einen Bruttobetrag; None, wenn DATEV so nicht rechnen kann."""
    if function.automatic is not None:
        return "" if function.automatic_rate(kind) == rate else None
    key = TAX_KEYS.get((kind, rate))
    return key if key is not None and function.allows_tax_key(kind) else None


def _positions(entry, lines, groups: list[_TaxGroup], chart: str | None) -> list[_Position]:
    tax_by_base: dict[int, tuple[_TaxGroup, Decimal]] = {}
    grouped_tax_lines = set()
    for group in groups:
        grouped_tax_lines.add(id(group.tax_line))
        for base, share in group.bases:
            tax_by_base[id(base)] = (group, share)

    positions = []
    for line in lines:
        if id(line) in grouped_tax_lines:
            continue
        function = datev_account_function(chart, line.code)
        position = _Position(
            code=line.code,
            is_debit=_is_debit(line),
            amount=_line_amount(line),
            text=line.description or entry.description,
            function=function,
        )
        if id(line) in tax_by_base:
            group, share = tax_by_base[id(line)]
            position.group = group
            position.amount += share
            position.tax = share
            position.tax_key = _gross_tax_key(function, group.kind, group.rate) or ""
        positions.append(position)
    return positions


def _match(positions: list[_Position]) -> tuple[list[_Booking], _Position | None]:
    """Zerlegt eine Buchung in Buchungssätze mit Konto und Gegenkonto.

    Bruttopositionen werden zuerst gegen Positionen ohne Automatikfunktion
    verrechnet (ein Satz trägt nur einen BU-Schlüssel), danach alles Übrige in
    Zeilenreihenfolge. Rechnet DATEV die Steuer einer auf mehrere Sätze
    verteilten Bruttoposition anders, als gebucht, kommt sie als Fehlschlag zurück.
    """
    debit = [position for position in positions if position.is_debit]
    credit = [position for position in positions if not position.is_debit]
    gross_debit = [position for position in debit if position.is_gross]
    gross_credit = [position for position in credit if position.is_gross]
    if gross_debit and gross_credit:
        return [], gross_debit[0]
    gross, partners = (gross_debit, credit) if gross_debit else (gross_credit, debit)

    remaining = {id(position): position.amount for position in positions}
    bookings: list[_Booking] = []

    def book(first: _Position, second: _Position, amount: Decimal) -> None:
        debit_position, credit_position = (first, second) if first.is_debit else (second, first)
        bookings.append(_Booking(debit_position, credit_position, amount))
        remaining[id(first)] -= amount
        remaining[id(second)] -= amount

    for position in gross:
        for partner in partners:
            if remaining[id(position)] == ZERO:
                break
            if partner.function.automatic is not None or remaining[id(partner)] == ZERO:
                continue
            book(position, partner, min(remaining[id(position)], remaining[id(partner)]))
        if remaining[id(position)] > ZERO:
            return [], position

    open_debit = [position for position in debit if remaining[id(position)] > ZERO]
    open_credit = [position for position in credit if remaining[id(position)] > ZERO]
    while open_debit and open_credit:
        book(
            open_debit[0],
            open_credit[0],
            min(remaining[id(open_debit[0])], remaining[id(open_credit[0])]),
        )
        open_debit = [position for position in open_debit if remaining[id(position)] > ZERO]
        open_credit = [position for position in open_credit if remaining[id(position)] > ZERO]
    if open_debit or open_credit:
        raise ValueError("Buchung ist nicht ausgeglichen.")

    for position in gross:
        parts = [
            booking.amount
            for booking in bookings
            if position in (booking.debit, booking.credit)
        ]
        if len(parts) > 1 and (
            sum(datev_tax_from_gross(part, position.group.rate) for part in parts)
            != position.tax
        ):
            return [], position
    return bookings, None


def _entry_bookings(entry, lines, tax_codes, tax_accounts, chart) -> list[_Booking]:
    """Buchungssätze einer Buchung; Steuer im Brutto, wo DATEV sie exakt nachrechnet."""
    # Ohne passenden BU-Schlüssel (anderer Steuersatz, Sonderfunktion, KU-Konto)
    # bleibt eine Steuerzeile eigene Position.
    groups = [
        group
        for group in _tax_groups(lines, tax_codes, tax_accounts)
        if all(
            _gross_tax_key(datev_account_function(chart, base.code), group.kind, group.rate)
            is not None
            for base, _ in group.bases
        )
    ]
    while True:
        bookings, failed = _match(_positions(entry, lines, groups, chart))
        if failed is None:
            return bookings
        groups = [group for group in groups if group is not failed.group]


def _booking_row(booking: _Booking, *, entry: JournalEntry) -> str:
    """Ein Buchungssatz; das Soll-/Haben-Kennzeichen bezieht sich auf das Konto."""
    gross = next(
        (position for position in (booking.debit, booking.credit) if position.is_gross), None
    )
    automatic = [
        position
        for position in (booking.debit, booking.credit)
        if position.function.automatic is not None
    ]
    if gross is not None:
        contra = gross
        bu_key = gross.tax_key
    elif len(automatic) == 1:
        contra = automatic[0]
        bu_key = BU_SUSPEND_AUTOMATIC if contra.function.computes_tax else ""
    else:
        contra = booking.credit
        bu_key = (
            BU_SUSPEND_AUTOMATIC
            if any(position.function.computes_tax for position in automatic)
            else ""
        )
    account = booking.debit if contra is booking.credit else booking.credit
    fields = [
        _fmt_amount(booking.amount),
        _quote("S" if account.is_debit else "H"),
        _quote("EUR"),
        "",  # Kurs
        "",  # Basis-Umsatz
        _quote(""),  # WKZ Basis-Umsatz
        account.code,
        contra.code,
        _quote(bu_key),
        _fmt_beleg_date(entry.entry_date),
        _quote(_clip(entry.posting_number, 36)),
        _quote(""),  # Belegfeld 2
        "",  # Skonto
        _quote(_clip(contra.text, 60)),
    ]
    return ";".join(fields)


def build_datev_export(
    *,
    session: Session,
    company_id: int,
    options: DatevExportOptions | None = None,
    generated_at: datetime,
    chart: str | None = None,
) -> str:
    """Erzeugt den EXTF-Buchungsstapel als String für eine Gesellschaft.

    ``chart`` ("skr03"/"skr04") legt den Kontenrahmen für Automatikkonten und
    Steuerschlüssel fest; ohne Angabe wird er an den Konten erkannt.
    """
    options = options or DatevExportOptions()
    if chart is None:
        chart = detect_company_chart(session=session, company_id=company_id)

    entries = (
        session.execute(
            select(JournalEntry)
            .where(JournalEntry.company_id == company_id)
            .order_by(JournalEntry.entry_date, JournalEntry.id)
        )
        .scalars()
        .all()
    )

    line_rows = session.execute(
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntryLine.line_number,
            JournalEntryLine.account_id,
            JournalEntryLine.tax_code_id,
            Account.code,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.description,
        )
        .join(Account, Account.id == JournalEntryLine.account_id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(JournalEntry.company_id == company_id)
        .order_by(JournalEntryLine.journal_entry_id, JournalEntryLine.line_number)
    ).all()

    lines_by_entry: dict[int, list] = {}
    for row in line_rows:
        lines_by_entry.setdefault(row.journal_entry_id, []).append(row)

    tax_codes = {
        tax_code.id: tax_code
        for tax_code in session.execute(select(TaxCode).where(TaxCode.company_id == company_id))
        .scalars()
        .all()
    }
    # Steuerkonten der Steuercodes; ein Konto mit widersprüchlichen Codes zählt nicht.
    rates_by_account: dict[int, set[tuple[str, Decimal]]] = {}
    for tax_code in tax_codes.values():
        if tax_code.vat_account_id is not None and tax_code.rate > ZERO:
            rates_by_account.setdefault(tax_code.vat_account_id, set()).add(
                (tax_code.kind, tax_code.rate)
            )
    tax_accounts = {
        account_id: next(iter(rates))
        for account_id, rates in rates_by_account.items()
        if len(rates) == 1
    }

    dates = [entry.entry_date for entry in entries]
    fiscal_year_start = date(min(dates).year, 1, 1) if dates else date(generated_at.year, 1, 1)

    buffer = StringIO()
    buffer.write(
        _header_row(
            options=options,
            generated_at=generated_at,
            fiscal_year_start=fiscal_year_start,
            date_from=min(dates) if dates else None,
            date_to=max(dates) if dates else None,
            finalized=bool(entries) and all(entry.is_finalized for entry in entries),
            chart=chart,
        )
    )
    buffer.write("\r\n")
    buffer.write(";".join(_quote(column) for column in DATA_COLUMNS))
    buffer.write("\r\n")

    for entry in entries:
        lines = lines_by_entry.get(entry.id, [])
        for booking in _entry_bookings(entry, lines, tax_codes, tax_accounts, chart):
            buffer.write(_booking_row(booking, entry=entry))
            buffer.write("\r\n")

    return buffer.getvalue()
