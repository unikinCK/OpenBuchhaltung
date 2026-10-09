"""Erzeugt ``data/kontenrahmen/datev_kontenfunktionen.csv`` aus den DATEV-Kontenrahmen.

Quelle sind die DATEV-Kontenrahmen „Gültig für 2026“ als PDF (SKR03 Art.-Nr. 11174,
SKR04 Art.-Nr. 11175, frei auf datev.de). Der DATEV-Export braucht daraus zweierlei:

* Hauptfunktionen AM/AV (auch S/AM, S/AV): Automatikkonten, auf denen DATEV die
  Umsatz- bzw. Vorsteuer selbst aus dem Bruttobetrag errechnet (Legende am
  Dokumentende: „Automatische Errechnung der Umsatzsteuer/Vorsteuer“).
* Zusatzfunktionen KU/V/M über den Kontenklassen: keine Steuerrechnung möglich,
  nur Vorsteuer bzw. nur Umsatzsteuer zulässig.

Die Spalte ``steuer`` fasst zusammen, was DATEV auf einem Automatikkonto rechnet:
ein Steuersatz (z. B. ``19``), ``frei`` (keine Steuer, nur Kennzahl-Zuordnung) oder
``sonder`` (zwei Steuern wie beim innergemeinschaftlichen Erwerb und § 13b UStG als
Leistungsempfänger, Skontokonten ohne Steuersatz). Die Spalte ``kennzahl`` nennt die
UStVA-Kennzahl der Bemessungsgrundlage, abgeleitet aus der Bezeichnung (z. B. 41 für
steuerfreie innergemeinschaftliche Lieferungen, 21 für sonstige Leistungen nach
§ 18b, 46 für § 13b-Leistungen eines EU-Unternehmers); leer, wo die Bezeichnung
keine eindeutige Kennzahl ergibt.

Aufruf mit beiden PDFs (pypdf ist Laufzeitabhängigkeit):

    .venv/bin/python tools/datev_kontenfunktionen.py \\
        "11174 SKR03 BilrUg.pdf" "11175 SKR04 BilrUg.pdf"
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

OUTPUT = (
    Path(__file__).resolve().parents[1] / "data" / "kontenrahmen" / "datev_kontenfunktionen.csv"
)

ACCOUNT_LINE = re.compile(r"^\s*(?:[UGKF]\s+)*(?:(?:S/AM|S/AV|AM|AV|S|F|R)\s+)?\d{4}\b")
AUTOMATIC_LINE = re.compile(r"^\s*(?:[UGK]\s+)*(?:S/)?(AM|AV)\s+(\d{4})\b\s*(.*)$")
RANGE_LINE = re.compile(r"^\s*-\s*(\d{2})\s*(.*)$")
RESTRICTION_LINE = re.compile(r"\s*(?:(?:KU|V|M)\s+\d{4}(?:-\d{4})?(?:\s+\d{1,2}\))?\s*)+")
RESTRICTION_TOKEN = re.compile(r"(KU|V|M)\s+(\d{4})(?:-(\d{4}))?")
# Zeilen, mit denen eine Kontobezeichnung sicher endet (Seitenfuß, Spaltenköpfe).
STOP_LINE = re.compile(
    r"^(Art\.-Nr\.|A$|GuV-|Bilanz-|Programm-|Abschluss-|verbindung|zweck|Posten|\s*$)"
)
# Überschriften der Bilanz-/GuV-Spalte, die der Text in Bezeichnungen mischt.
HEADINGS = (
    "Sonstige Vermögensgegenstände oder Sonstige Verbindlichkeiten",
    "Erhaltene Anzahlungen auf Bestellungen (von Vorräten offen abgesetzt)",
    "Sonstige Zinsen und ähnliche Erträge",
)
WORD_FIXES = {
    "Vorsteue r": "Vorsteuer",
    "f ür": "für",
    "s onstigen": "sonstigen",
    "War en": "Waren",
}

# „ohne Vorsteuerabzug“ (steuerfreie Umsätze) ist keine zweite Steuer.
TWO_TAXES = re.compile(r"Vorsteuer und|ohne Vorsteuer\b|mit Umsatzsteuer|als Leistungsempfänger")
SELLER_REVERSE_CHARGE = "Leistungsempfänger die Umsatzsteuer"
TAX_FREE = re.compile(
    r"steuerfrei|Steuerfrei|0 % USt|nicht steuerbar|Lieferungen des ersten Abnehmers"
)
RATE = re.compile(r"(\d+(?:,\d+)?) % (USt|Vorsteuer|VSt)")
# Muster für die UStVA-Kennzahl steuerfreier Automatikkonten (kleingeschrieben).
INTRA_EU_SUPPLY = re.compile(
    r"innergemeinschaftliche\w* lieferung|§ 4 nr\. 1b|steuerfreien eu-lieferung"
)
TAX_FREE_WITH_INPUT_TAX = re.compile(
    r"§ 4 nr\. 1a|§ 4 nr\. [2-7]\b|nr\. 2 bis 7|offshore|mit vorsteuerabzug"
)
TAX_FREE_WITHOUT_INPUT_TAX = re.compile(
    r"§ 4 nr\. 8|§ 4 nr\. 12|ohne vorsteuerabzug|steuerfreie umsätze inland"
)


def _join_hyphenation(match: re.Match[str]) -> str:
    word = match.group(1)
    if word in ("und", "oder", "bzw"):
        return f"- {word}"
    return f"-{word}" if word[0].isupper() else word


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    for wrong, right in WORD_FIXES.items():
        name = name.replace(wrong, right)
    # Silbentrennung: „Wirt- schaftsgüter“ zusammenziehen, „Hilfs- und“ und
    # „Fahrzeug-Nutzung“ (Bindestrich vor Großbuchstaben) erhalten.
    name = re.sub(r"-\s+(\w+)", _join_hyphenation, name)
    for heading in HEADINGS:
        name = name.replace(heading, "")
    # Fußnotenmarken („USt1)“, „UStG 9)“) und Spaltenreste (SB, EÜR, Programmverbindung).
    name = re.sub(r"\s?\d{1,2}\)", "", name)
    name = re.sub(r"(\s+(?:SB|HB|EÜR|U|G|K))+$", "", name)
    return re.sub(r"\s+", " ", name).strip()


def classify(function: str, name: str) -> str:
    """Steuerrechnung der Automatik: Steuersatz, ``frei`` oder ``sonder``."""
    if TWO_TAXES.search(name):
        return "sonder"
    if SELLER_REVERSE_CHARGE in name or TAX_FREE.search(name):
        return "frei"
    rates = {match.group(1) for match in RATE.finditer(name)}
    kinds = {match.group(2) for match in RATE.finditer(name)}
    expected_kinds = {"USt"} if function == "AM" else {"Vorsteuer", "VSt"}
    if len(rates) != 1 or not kinds <= expected_kinds:
        return "sonder"
    rate = Decimal(rates.pop().replace(",", "."))
    return format(rate.normalize(), "f")


def ustva_kennzahl(function: str, tax: str, name: str) -> str:
    """UStVA-Kennzahl der Bemessungsgrundlage eines Automatikkontos ("" = keine eindeutige)."""
    lower = name.casefold()
    if function == "AM":
        if tax not in ("frei", "sonder"):
            return {"19": "81", "7": "86"}.get(tax, "35")
        if "neufahrzeug" in lower:
            return "44"
        if "dreiecksgesch" in lower:
            return "42"
        if INTRA_EU_SUPPLY.search(lower):
            return "41"
        if "leistungsempfänger die umsatzsteuer" in lower:
            return "21" if "eu-land" in lower else "60"
        if "nicht steuerbar" in lower:
            return "45"
        if "0 % ust" in lower:
            return "87"
        if TAX_FREE_WITH_INPUT_TAX.search(lower):
            return "43"
        if TAX_FREE_WITHOUT_INPUT_TAX.search(lower):
            return "48"
        return ""
    if "neufahrzeug" in lower or "steuerfreier" in lower:
        return ""
    if "innergemeinschaftlich" in lower and "erwerb" in lower:
        rate = re.search(r"(\d+) % umsatzsteuer", lower)
        return {"19": "89", "7": "93"}.get(rate.group(1), "") if rate else ""
    if "im anderen eu-land ansässigen unternehmers" in lower:
        return "46"
    if re.search(r"im ausland ansässigen unternehmers|bauleistungen|§ 13b", lower):
        return "84"
    return ""


def _text(pdf: Path) -> str:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
    # „U A“ am Zeilenende + „M 8850 …“: die Funktion ist über zwei Zeilen verteilt.
    return re.sub(r"(\n\s*(?:[UGK]\s+)*)A\s*\n\s*([MV]\s+\d{4})", r"\1A\2", text)


def parse(pdf: Path) -> list[tuple[str, str, str, str, str, str]]:
    lines = _text(pdf).splitlines()
    rows: list[tuple[str, str, str, str, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if RESTRICTION_LINE.fullmatch(line):
            for function, start, end in RESTRICTION_TOKEN.findall(line):
                rows.append((start, end or start, function, "", "", ""))
            index += 1
            continue
        match = AUTOMATIC_LINE.match(line)
        if not match:
            index += 1
            continue
        function, start, rest = match.groups()
        end = start
        parts = [rest] if rest.strip() else []
        index += 1
        if index < len(lines) and (range_match := RANGE_LINE.match(lines[index])):
            end = start[:2] + range_match.group(1)
            if range_match.group(2).strip():
                parts.append(range_match.group(2))
            index += 1
        while index < len(lines):
            following = lines[index]
            if (
                ACCOUNT_LINE.match(following)
                or STOP_LINE.match(following)
                or RESTRICTION_LINE.fullmatch(following)
            ):
                break
            parts.append(following)
            index += 1
        name = _clean_name(" ".join(parts))
        tax = classify(function, name)
        rows.append((start, end, function, tax, ustva_kennzahl(function, tax, name), name))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("skr03_pdf", type=Path, help="DATEV-Kontenrahmen SKR03 (Art.-Nr. 11174)")
    parser.add_argument("skr04_pdf", type=Path, help="DATEV-Kontenrahmen SKR04 (Art.-Nr. 11175)")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)

    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["kontenrahmen", "von", "bis", "funktion", "steuer", "kennzahl", "bezeichnung"]
        )
        for chart, pdf in (("skr03", args.skr03_pdf), ("skr04", args.skr04_pdf)):
            rows = sorted(set(parse(pdf)), key=lambda row: (row[0], row[2]))
            for start, end, function, tax, kennzahl, name in rows:
                writer.writerow([chart, start, end, function, tax, kennzahl, name])
            automatic = sum(1 for row in rows if row[2] in ("AM", "AV"))
            print(f"{chart}: {automatic} Automatikkonten, {len(rows) - automatic} Zusatzfunktionen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
