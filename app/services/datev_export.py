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

1. Steuerzeilen (dieselben Steuerkonten wie in der UStVA, ``company_tax_accounts``:
   Steuerkonten der Steuercodes, sonst die Standard-Steuerkonten 1571/1576/1771/1776
   bzw. 1401/1406/3801/3806) werden ihrer Bemessungsgrundlage
   zugeordnet: einer Zeile derselben Seite, deren Bruttobetrag DATEV auf genau
   diese Steuer zurückrechnet (kaufmännisch gerundet). Beide werden zu einem
   Bruttobetrag zusammengefasst.
2. Auf einem Automatikkonto mit passendem Steuersatz trägt der Buchungssatz
   keinen BU-Schlüssel; auf anderen Konten den Steuerschlüssel 101/102
   (Umsatzsteuer 19/7 %) bzw. 401/402 (Vorsteuer 19/7 %), sofern der
   Kontenrahmen dort Steuer dieser Art zulässt (Zusatzfunktion KU/V/M).
3. Weicht DATEVs Rechnung um wenige Cent ab (einzeln gerundete Rechnungs-
   positionen, Aufteilung auf mehrere Gegenkonten: höchstens 5 Cent und 1 % der
   Steuer), bleibt der Satz brutto und ein Korrektursatz „Steuer-Rundungsdifferenz“
   zwischen Konto und DATEV-Steuerkonto gleicht die Differenz aus. Geht es auch so
   nicht auf (anderer Steuersatz, Sonderfunktion wie innergemeinschaftlicher
   Erwerb, größere Abweichung), bleiben Netto- und Steuerzeile getrennt; der Satz
   auf dem Automatikkonto trägt dann den BU-Schlüssel 40. Ebenso Automatikkonten
   ohne Steuerzeile (Abschluss-, Umbuchungen, Saldovorträge) – außer steuerfreien
   Automatikkonten, deren Funktion nur die UStVA-Kennzahl bestimmt.
4. Mehrzeilige Buchungen werden in Buchungssätze mit Konto und Gegenkonto
   zerlegt und über das gemeinsame Belegfeld 1 (Buchungsnummer) gruppiert.
   Ein Buchungssatz mit Steuer führt das Sachkonto der Steuer als Gegenkonto
   (Konto des Steuerschlüssels, „BU Gegenkonto“ in DATEV).
5. Steuerkonten des jeweils anderen Kontenrahmens (Altimport, z. B. SKR04 1406 in
   einer SKR03-Buchhaltung) stehen im Stapel unter der Nummer des erkannten
   Rahmens (1576); Sätze, die danach Konto und Gegenkonto gleich hätten (die
   Umbuchung 1406 → 1576), entfallen.

Welche Konten Automatikkonten sind, hängt vom Kontenrahmen ab (SKR04 4400 ist
Erlöse 19 % USt, SKR03 4400 frei verfügbar). Der Export erkennt ihn wie die
Kontenrahmen-Prüfung (``detect_company_chart``) und schreibt ihn in das
Kopffeld „Sachkontenrahmen“; die Kontenfunktionen stehen in
``data/kontenrahmen/datev_kontenfunktionen.csv`` (erzeugt mit
``tools/datev_kontenfunktionen.py``).

Ein Stapel umfasst genau ein Wirtschaftsjahr: Das Belegdatum hat das Format
TTMM, „das Jahr wird immer aus dem Feld #13 des Headers ermittelt“ (WJ-Beginn),
und Datum von/bis (Kopffelder 15/16) begrenzen den Stapel; DATEV empfiehlt eine
Datei je Buchungsperiode (DATEV-Formatbeschreibung, Header und Buchungsstapel).
Exportiert werden deshalb die Buchungen eines Wirtschaftsjahres der Gesellschaft
oder eines Zeitraums darin (``resolve_datev_export_period``); das
Festschreibekennzeichen (Kopffeld 21) gilt nur für diese Buchungen.

Der Export ist bewusst als "DATEV-kompatibler" Stapel ausgelegt (nicht
zertifiziert).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from io import StringIO
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.account_chart_check import detect_company_chart
from app.services.datev_account_functions import (
    DatevAccountFunction,
    datev_account_function,
)
from app.services.journal_entries import fiscal_year_bounds
from app.services.tax_codes import DEFAULT_TAX_CODES, TaxAccount, company_tax_accounts
from domain.models import (
    TAX_KIND_INPUT,
    TAX_KIND_OUTPUT,
    Account,
    Company,
    FiscalYear,
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
# Steuerkonten, auf die DATEV Steuer je Art und Satz bucht (Reihenfolge der
# Standardkonten in DEFAULT_TAX_CODES: SKR03, SKR04).
CHART_TAX_ACCOUNTS: dict[str, dict[tuple[str, Decimal], str]] = {
    chart: {
        (default.kind, default.rate): default.vat_account_codes[index]
        for default in DEFAULT_TAX_CODES
        if default.vat_account_codes
    }
    for index, chart in enumerate(("skr03", "skr04"))
}
# Steuerkonten des jeweils anderen Kontenrahmens (Altimporte wie SKR04 1406 in
# einer SKR03-Buchhaltung) exportiert der Stapel unter der Nummer des erkannten
# Rahmens – dort ist die fremde Nummer nicht oder anders vergeben.
FOREIGN_TAX_ACCOUNT_CODES: dict[str, dict[str, str]] = {
    "skr03": {
        default.vat_account_codes[1]: default.vat_account_codes[0]
        for default in DEFAULT_TAX_CODES
        if default.vat_account_codes
    },
    "skr04": {
        default.vat_account_codes[0]: default.vat_account_codes[1]
        for default in DEFAULT_TAX_CODES
        if default.vat_account_codes
    },
}
# Weicht die gebuchte Steuer um wenige Cent von DATEVs Rechnung aus dem Brutto ab
# (einzeln gerundete Rechnungspositionen), bleibt der Satz brutto und ein
# Korrektursatz gleicht die Differenz aus: höchstens 5 Cent und 1 % der Steuer.
MAX_ROUNDING_DIFFERENCE = Decimal("0.05")
ROUNDING_TEXT = "Steuer-Rundungsdifferenz"


@dataclass(slots=True)
class DatevExportOptions:
    consultant_number: int = 1000  # Beraternummer
    client_number: int = 1  # Mandantennummer
    account_length: int = 4  # Sachkontennummernlänge
    description: str = "OpenBuchhaltung Buchungsstapel"


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
    # Gebuchte minus von DATEV aus dem Brutto errechnete Steuer (Korrektursatz).
    correction: Decimal = ZERO

    @property
    def is_gross(self) -> bool:
        return self.group is not None


@dataclass(slots=True)
class _Booking:
    """Ein DATEV-Buchungssatz: Soll- und Habenposition mit Betrag."""

    debit: _Position
    credit: _Position
    amount: Decimal


def _rounding_tolerance(chart: str | None, kind: str, rate: Decimal, tax: Decimal) -> Decimal:
    """Rundungsdifferenz, die ein Korrektursatz ausgleichen darf (0 ohne DATEV-Steuerkonto)."""
    if (kind, rate) not in CHART_TAX_ACCOUNTS.get(chart or "", {}):
        return ZERO
    return min(MAX_ROUNDING_DIFFERENCE, (abs(tax) / HUNDRED).quantize(CENT, rounding=ROUND_DOWN))


def _line_amount(line) -> Decimal:
    return line.debit_amount if line.debit_amount > ZERO else line.credit_amount


def _is_debit(line) -> bool:
    return line.debit_amount > ZERO


def _tax_of_line(
    line, tax_codes: dict[int, TaxCode], tax_accounts: dict[int, TaxAccount]
) -> tuple[str, Decimal] | None:
    """(Art, Satz) einer Steuerzeile oder None für andere Zeilen."""
    tax_code = tax_codes.get(line.tax_code_id)
    if (
        tax_code is not None
        and tax_code.vat_account_id == line.account_id
        and tax_code.rate > ZERO
    ):
        return tax_code.kind, tax_code.rate
    tax_account = tax_accounts.get(line.account_id)
    if tax_account is None or tax_account.rate is None:
        return None
    return tax_account.kind, tax_account.rate


def _tax_groups(
    lines, tax_codes: dict[int, TaxCode], tax_accounts, chart: str | None
) -> list[_TaxGroup]:
    """Ordnet die Steuerzeilen einer Buchung ihren Bemessungsgrundlagen zu.

    Passt eine einzelne Zeile derselben Seite (bei gleichem Steuercode
    bevorzugt), deren Brutto DATEV auf diese Steuer zurückrechnet – exakt oder bis
    auf eine Rundungsdifferenz, die ein Korrektursatz ausgleicht –, gewinnt die
    mit der kleinsten Differenz, dann die nächstgelegene davor. Sonst darf sich
    die Steuerzeile auf mehrere Grundlagen verteilen, wenn deren einzeln
    gerundete Steuern sie genau ergeben. Was nicht aufgeht, bleibt eigene Steuerzeile.
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
        differences = {
            id(line): abs(
                tax_amount - datev_tax_from_gross(_line_amount(line) + tax_amount, rate)
            )
            for line in candidates
        }
        tolerance = _rounding_tolerance(chart, kind, rate, tax_amount)
        singles = [line for line in candidates if differences[id(line)] <= tolerance]
        if singles:
            smallest = min(differences[id(line)] for line in singles)
            singles = [line for line in singles if differences[id(line)] == smallest]
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


def _export_code(line, tax_accounts, chart: str | None) -> str:
    """Kontonummer im Stapel: Steuerkonten des anderen Kontenrahmens unter der eigenen Nummer."""
    if line.account_id in tax_accounts:
        return FOREIGN_TAX_ACCOUNT_CODES.get(chart or "", {}).get(line.code, line.code)
    return line.code


def _net_same_accounts(positions: list[_Position]) -> list[_Position]:
    """Verrechnet ein Konto, das auf Soll und Haben steht (z. B. die Umbuchung
    1406 → 1576, die nach der Abbildung auf 1576 nichts mehr bewegt)."""
    sides: dict[str, set[bool]] = {}
    for position in positions:
        if not position.is_gross:
            sides.setdefault(position.code, set()).add(position.is_debit)
    both = {code for code, flags in sides.items() if len(flags) == 2}
    result: list[_Position] = []
    netted: set[str] = set()
    for position in positions:
        if position.is_gross or position.code not in both:
            result.append(position)
            continue
        if position.code in netted:
            continue
        netted.add(position.code)
        balance = sum(
            (other.amount if other.is_debit else -other.amount)
            for other in positions
            if not other.is_gross and other.code == position.code
        )
        if balance != ZERO:
            position.is_debit = balance > ZERO
            position.amount = abs(balance)
            result.append(position)
    return result


def _positions(
    entry, lines, groups: list[_TaxGroup], chart: str | None, tax_accounts
) -> list[_Position]:
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
        code = _export_code(line, tax_accounts, chart)
        function = datev_account_function(chart, code)
        position = _Position(
            code=code,
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
    return _net_same_accounts(positions)


def _match(
    positions: list[_Position], chart: str | None
) -> tuple[list[_Booking], _Position | None]:
    """Zerlegt eine Buchung in Buchungssätze mit Konto und Gegenkonto.

    Bruttopositionen werden zuerst gegen Positionen ohne Automatikfunktion
    verrechnet (ein Satz trägt nur einen BU-Schlüssel), danach alles Übrige in
    Zeilenreihenfolge. Weicht die Steuer, die DATEV aus den Sätzen einer
    Bruttoposition errechnet, um mehr als eine Rundungsdifferenz von der
    gebuchten ab, kommt die Position als Fehlschlag zurück; sonst merkt sie sich
    die Differenz für den Korrektursatz.
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
        group = position.group
        datev_tax = sum(
            datev_tax_from_gross(booking.amount, group.rate)
            for booking in bookings
            if position in (booking.debit, booking.credit)
        )
        difference = position.tax - datev_tax
        if abs(difference) > _rounding_tolerance(chart, group.kind, group.rate, position.tax):
            return [], position
        position.correction = difference
    return bookings, None


def _correction_booking(position: _Position, chart: str | None, entry) -> _Booking:
    """Gleicht die Rundungsdifferenz einer Bruttoposition zwischen Konto und Steuerkonto aus.

    DATEV bucht aus dem Brutto ``tax − correction`` auf sein Steuerkonto; der Satz
    verschiebt ``correction`` dorthin (positiv) bzw. zurück aufs Konto (negativ).
    """
    group = position.group
    tax_code = CHART_TAX_ACCOUNTS[chart][(group.kind, group.rate)]
    text = f"{ROUNDING_TEXT} {entry.description or ''}"
    amount = abs(position.correction)
    tax_position = _Position(
        code=tax_code,
        is_debit=position.is_debit == (position.correction > ZERO),
        amount=amount,
        text=text,
        function=datev_account_function(chart, tax_code),
    )
    base_position = _Position(
        code=position.code,
        is_debit=not tax_position.is_debit,
        amount=amount,
        text=text,
        function=position.function,
    )
    if tax_position.is_debit:
        return _Booking(tax_position, base_position, amount)
    return _Booking(base_position, tax_position, amount)


def _entry_bookings(entry, lines, tax_codes, tax_accounts, chart) -> list[_Booking]:
    """Buchungssätze einer Buchung; Steuer im Brutto, wo DATEV sie (bis auf einen
    Korrektursatz für Rundungscent) nachrechnet."""
    # Ohne passenden BU-Schlüssel (anderer Steuersatz, Sonderfunktion, KU-Konto)
    # bleibt eine Steuerzeile eigene Position.
    groups = [
        group
        for group in _tax_groups(lines, tax_codes, tax_accounts, chart)
        if all(
            _gross_tax_key(datev_account_function(chart, base.code), group.kind, group.rate)
            is not None
            for base, _ in group.bases
        )
    ]
    while True:
        positions = _positions(entry, lines, groups, chart, tax_accounts)
        bookings, failed = _match(positions, chart)
        if failed is None:
            return bookings + [
                _correction_booking(position, chart, entry)
                for position in positions
                if position.is_gross and position.correction != ZERO
            ]
        groups = [group for group in groups if group is not failed.group]


def _booking_row(booking: _Booking, *, entry: JournalEntry) -> str | None:
    """Ein Buchungssatz; das Soll-/Haben-Kennzeichen bezieht sich auf das Konto.

    None für Sätze ohne Wirkung (Konto gleich Gegenkonto, ohne Steuer).
    """
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
    if account.code == contra.code and gross is None:
        return None
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


class DatevExportError(ValueError):
    """Wirtschaftsjahr oder Zeitraum des Stapels ist ungültig oder nicht eindeutig."""

    def __init__(self, message: str, *, fiscal_years: list[DatevFiscalYear] | None = None):
        super().__init__(message)
        # Zur Auswahl, wenn ohne Angabe mehrere Wirtschaftsjahre in Frage kommen.
        self.fiscal_years = fiscal_years or []


@dataclass(frozen=True, slots=True)
class DatevFiscalYear:
    """Wirtschaftsjahr zur Auswahl des Stapels, mit der Anzahl seiner Buchungen."""

    id: int
    label: str
    start_date: date
    end_date: date
    entry_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "entry_count": self.entry_count,
        }


@dataclass(frozen=True, slots=True)
class DatevExportPeriod:
    """Wirtschaftsjahr und Zeitraum eines Buchungsstapels."""

    fiscal_year_id: int | None  # None: noch kein Wirtschaftsjahr angelegt
    label: str
    fiscal_year_start: date  # Kopffeld 13 "WJ-Beginn"
    fiscal_year_end: date
    date_from: date  # Kopffeld 15 "Datum von"
    date_to: date  # Kopffeld 16 "Datum bis"


def datev_fiscal_years(*, session: Session, company_id: int) -> list[DatevFiscalYear]:
    """Wirtschaftsjahre der Gesellschaft, das jüngste zuerst, mit Anzahl der Buchungen."""
    entry_counts = dict(
        session.execute(
            select(JournalEntry.fiscal_year_id, func.count(JournalEntry.id))
            .where(JournalEntry.company_id == company_id)
            .group_by(JournalEntry.fiscal_year_id)
        ).all()
    )
    fiscal_years = session.execute(
        select(FiscalYear)
        .where(FiscalYear.company_id == company_id)
        .order_by(FiscalYear.start_date.desc())
    ).scalars()
    return [
        DatevFiscalYear(
            id=fiscal_year.id,
            label=fiscal_year.label,
            start_date=fiscal_year.start_date,
            end_date=fiscal_year.end_date,
            entry_count=entry_counts.get(fiscal_year.id, 0),
        )
        for fiscal_year in fiscal_years
    ]


def resolve_datev_export_period(
    *,
    session: Session,
    company_id: int,
    fiscal_year_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    today: date | None = None,
) -> DatevExportPeriod:
    """Wirtschaftsjahr und Zeitraum des Stapels zur Auswahl.

    * ``fiscal_year_id``: dieses Wirtschaftsjahr; ``date_from``/``date_to``
      grenzen den Zeitraum darin ein (sonst das ganze Wirtschaftsjahr).
    * nur ``date_from``/``date_to``: das Wirtschaftsjahr, in dem sie liegen.
    * keine Angabe: das Wirtschaftsjahr mit Buchungen. Haben mehrere
      Wirtschaftsjahre Buchungen, ist die Auswahl Pflicht – ein stiller
      Standard übergäbe leicht das falsche Jahr an DATEV. Ohne Buchungen gilt
      das jüngste Wirtschaftsjahr, ohne Wirtschaftsjahr das reguläre zum Tag
      ``today`` (leerer Stapel).

    Ein Zeitraum über die Grenze des Wirtschaftsjahres ist ein Fehler: DATEV
    liest das Jahr des Belegdatums aus dem WJ-Beginn.
    """
    if date_from and date_to and date_from > date_to:
        raise DatevExportError(
            f"Das Datum von ({date_from.isoformat()}) liegt nach dem Datum bis "
            f"({date_to.isoformat()})."
        )

    if fiscal_year_id is not None:
        fiscal_year = session.get(FiscalYear, fiscal_year_id)
        if fiscal_year is None or fiscal_year.company_id != company_id:
            raise DatevExportError("Wirtschaftsjahr nicht gefunden.")
    elif date_from or date_to:
        anchor = date_from or date_to
        fiscal_year = (
            session.execute(
                select(FiscalYear).where(
                    FiscalYear.company_id == company_id,
                    FiscalYear.start_date <= anchor,
                    FiscalYear.end_date >= anchor,
                )
            )
            .scalars()
            .first()
        )
        if fiscal_year is None:
            raise DatevExportError(
                f"Für den {anchor.isoformat()} ist kein Wirtschaftsjahr angelegt."
            )
    else:
        fiscal_years = datev_fiscal_years(session=session, company_id=company_id)
        with_entries = [year for year in fiscal_years if year.entry_count]
        if len(with_entries) > 1:
            raise DatevExportError(
                "Buchungen in mehreren Wirtschaftsjahren ("
                + ", ".join(year.label for year in with_entries)
                + "); DATEV erwartet einen Stapel je Wirtschaftsjahr. Bitte das "
                "Wirtschaftsjahr (fiscal_year_id) oder einen Zeitraum angeben.",
                fiscal_years=with_entries,
            )
        if not fiscal_years:
            company = session.get(Company, company_id)
            start, end, label = fiscal_year_bounds(
                company.fiscal_year_start_month if company else 1, today or date.today()
            )
            return DatevExportPeriod(
                fiscal_year_id=None,
                label=label,
                fiscal_year_start=start,
                fiscal_year_end=end,
                date_from=start,
                date_to=end,
            )
        fiscal_year = session.get(FiscalYear, (with_entries or fiscal_years)[0].id)

    period_from = date_from or fiscal_year.start_date
    period_to = date_to or fiscal_year.end_date
    if not fiscal_year.start_date <= period_from <= period_to <= fiscal_year.end_date:
        if date_from and date_to:
            chosen = f"{date_from.isoformat()} – {date_to.isoformat()}"
        else:
            chosen = f"ab {date_from.isoformat()}" if date_from else f"bis {date_to.isoformat()}"
        raise DatevExportError(
            f"Der gewählte Zeitraum ({chosen}) liegt nicht im Wirtschaftsjahr "
            f"{fiscal_year.label} ({fiscal_year.start_date.isoformat()} – "
            f"{fiscal_year.end_date.isoformat()}). DATEV liest das Jahr des Belegdatums aus "
            "dem WJ-Beginn; je Wirtschaftsjahr ist ein eigener Stapel zu exportieren."
        )
    return DatevExportPeriod(
        fiscal_year_id=fiscal_year.id,
        label=fiscal_year.label,
        fiscal_year_start=fiscal_year.start_date,
        fiscal_year_end=fiscal_year.end_date,
        date_from=period_from,
        date_to=period_to,
    )


def datev_export_file_name(company_id: int, period: DatevExportPeriod) -> str:
    """Dateiname mit Wirtschaftsjahr und Zeitraum.

    ``EXTF_`` kennzeichnet in DATEV Daten aus einem Nicht-DATEV-Programm; die
    WJ-Bezeichnung wird auf ASCII-Buchstaben und -Ziffern reduziert
    (``2026/2027`` → ``2026-2027``).
    """
    label = re.sub(r"[^0-9A-Za-z]+", "-", period.label).strip("-")
    return (
        f"EXTF_Buchungsstapel_{company_id}_WJ{label or period.fiscal_year_start.year}_"
        f"{period.date_from:%Y%m%d}-{period.date_to:%Y%m%d}.csv"
    )


def build_datev_export(
    *,
    session: Session,
    company_id: int,
    options: DatevExportOptions | None = None,
    generated_at: datetime,
    chart: str | None = None,
    period: DatevExportPeriod | None = None,
) -> str:
    """Erzeugt den EXTF-Buchungsstapel als String für eine Gesellschaft.

    ``chart`` ("skr03"/"skr04") legt den Kontenrahmen für Automatikkonten und
    Steuerschlüssel fest; ohne Angabe wird er an den Konten erkannt.
    ``period`` (``resolve_datev_export_period``) wählt Wirtschaftsjahr und
    Zeitraum; ohne Angabe gilt die Standardauswahl (``DatevExportError``, wenn
    mehrere Wirtschaftsjahre Buchungen haben).
    """
    options = options or DatevExportOptions()
    if chart is None:
        chart = detect_company_chart(session=session, company_id=company_id)
    if period is None:
        period = resolve_datev_export_period(
            session=session, company_id=company_id, today=generated_at.date()
        )
    in_period = (
        JournalEntry.company_id == company_id,
        JournalEntry.fiscal_year_id == period.fiscal_year_id,
        JournalEntry.entry_date >= period.date_from,
        JournalEntry.entry_date <= period.date_to,
    )

    entries = (
        session.execute(
            select(JournalEntry)
            .where(*in_period)
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
        .where(*in_period)
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
    tax_accounts = company_tax_accounts(session=session, company_id=company_id)

    buffer = StringIO()
    buffer.write(
        _header_row(
            options=options,
            generated_at=generated_at,
            fiscal_year_start=period.fiscal_year_start,
            date_from=period.date_from,
            date_to=period.date_to,
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
            row = _booking_row(booking, entry=entry)
            if row is not None:
                buffer.write(row)
                buffer.write("\r\n")

    return buffer.getvalue()
