"""Kontenfunktionen der DATEV-Kontenrahmen SKR03/SKR04 (Stand 2026).

Quelle sind die DATEV-Kontenrahmen „Gültig für 2026“ (SKR03 Art.-Nr. 11174, SKR04
Art.-Nr. 11175), aufbereitet in ``data/kontenrahmen/datev_kontenfunktionen.csv`` mit
``tools/datev_kontenfunktionen.py``:

* Hauptfunktion AM/AV: Automatikkonten, auf denen DATEV die Umsatz- bzw. Vorsteuer
  selbst aus dem Bruttobetrag errechnet (DATEV-Export).
* Zusatzfunktion KU/V/M der Kontenklasse: keine Steuer, nur Vorsteuer, nur Umsatzsteuer.
* UStVA-Kennzahl der Bemessungsgrundlage eines Automatikkontos, etwa 41 für
  steuerfreie innergemeinschaftliche Lieferungen (8125/4125), 21 für sonstige
  Leistungen nach § 18b (8336/4336) oder 46 für § 13b-Leistungen eines
  EU-Unternehmers (3123/5923) – UStVA und Zusammenfassende Meldung ordnen Erlöse
  ohne Steuer darüber zu.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from domain.models import TAX_KIND_INPUT, TAX_KIND_OUTPUT

ACCOUNT_FUNCTIONS_CSV = (
    Path(__file__).resolve().parents[2] / "data" / "kontenrahmen" / "datev_kontenfunktionen.csv"
)


@dataclass(frozen=True, slots=True)
class DatevAccountFunction:
    """Steuerfunktionen eines Sachkontos laut DATEV-Kontenrahmen."""

    # Hauptfunktion "AM" (Umsatzsteuer-) bzw. "AV" (Vorsteuer-Automatik) oder None.
    automatic: str | None = None
    # Was die Automatik rechnet: Steuersatz ("19"), "frei" oder "sonder".
    tax: str | None = None
    # Zusatzfunktion der Kontenklasse: "KU" (keine Steuer), "V", "M" oder None.
    restriction: str | None = None
    # UStVA-Kennzahl der Bemessungsgrundlage des Automatikkontos (z. B. "41") oder None.
    kennzahl: str | None = None

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
def _account_function_table() -> dict[str, tuple[tuple[int, int, str, str, str], ...]]:
    table: dict[str, list[tuple[int, int, str, str, str]]] = {}
    with ACCOUNT_FUNCTIONS_CSV.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            table.setdefault(row["kontenrahmen"], []).append(
                (
                    int(row["von"]),
                    int(row["bis"]),
                    row["funktion"],
                    row["steuer"],
                    row["kennzahl"],
                )
            )
    return {chart: tuple(rows) for chart, rows in table.items()}


@lru_cache(maxsize=4096)
def datev_account_function(chart: str | None, code: str) -> DatevAccountFunction:
    """Kontenfunktion einer Sachkontonummer im SKR03/SKR04 (leer, wenn unbekannt)."""
    if chart is None or len(code) != 4 or not (code.isascii() and code.isdigit()):
        return DatevAccountFunction()
    number = int(code)
    automatic = tax = restriction = kennzahl = None
    for start, end, function, tax_info, kennzahl_info in _account_function_table().get(
        chart, ()
    ):
        if not start <= number <= end:
            continue
        if function in ("AM", "AV"):
            automatic, tax, kennzahl = function, tax_info, kennzahl_info or None
        else:
            restriction = function
    return DatevAccountFunction(
        automatic=automatic, tax=tax, restriction=restriction, kennzahl=kennzahl
    )
