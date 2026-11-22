"""Amtliche Lohnsteuerberechnung nach dem Programmablaufplan (PAP) des BMF.

Je Kalenderjahr gibt es ein aus dem amtlichen XML erzeugtes Modul
(``tools/pap_codegen.py``). Maßgebend ist das Jahr, in dem der
Lohnzahlungszeitraum endet (§ 38a Abs. 1 Satz 2 EStG).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import ModuleType
from typing import Mapping

from app.services.pap import lohnsteuer2026

PAP_MODULES: dict[int, ModuleType] = {
    2026: lohnsteuer2026,
}

# Lohnzahlungszeitraum (Eingabe LZZ des PAP).
LZZ_YEAR = 1
LZZ_MONTH = 2
LZZ_WEEK = 3
LZZ_DAY = 4

CENT = Decimal("0.01")


class PapNotAvailableError(ValueError):
    """Für das Jahr ist kein amtlicher PAP hinterlegt."""


@dataclass(frozen=True, slots=True)
class PapResult:
    year: int
    pap_name: str
    pap_version: str
    source_sha256: str
    # Euro-Beträge, aus den Cent-Ausgaben des PAP abgeleitet.
    wage_tax: Decimal
    solidarity_surcharge: Decimal
    church_tax_base: Decimal
    inputs: dict[str, object]
    outputs: dict[str, int]


def supported_years() -> list[int]:
    return sorted(PAP_MODULES)


def pap_module(year: int) -> ModuleType:
    module = PAP_MODULES.get(year)
    if module is None:
        years = ", ".join(str(item) for item in supported_years())
        raise PapNotAvailableError(
            f"Für {year} ist kein amtlicher Lohnsteuer-PAP hinterlegt (verfügbar: {years})."
        )
    return module


def calculate(year: int, inputs: Mapping[str, object]) -> PapResult:
    """Rechnet den PAP des Jahres mit den Eingaben (Beträge in Cent) durch."""
    module = pap_module(year)
    pap_class = getattr(module, module.PAP_NAME)
    outputs = pap_class(**dict(inputs)).calculate()
    return PapResult(
        year=year,
        pap_name=module.PAP_NAME,
        pap_version=module.PAP_VERSION,
        source_sha256=module.SOURCE_SHA256,
        wage_tax=_euro(outputs["LSTLZZ"]),
        solidarity_surcharge=_euro(outputs["SOLZLZZ"]),
        church_tax_base=_euro(outputs["BK"]),
        inputs={key: _jsonable(value) for key, value in inputs.items()},
        outputs=outputs,
    )


def _euro(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    return value
