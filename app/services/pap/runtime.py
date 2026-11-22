"""Laufzeit für die aus dem amtlichen PAP-XML erzeugten Lohnsteuer-Module.

Der Programmablaufplan des BMF ist in Java-Ausdrücken mit ``BigDecimal``
formuliert. ``BD`` bildet genau die dort benutzten Methoden mit derselben
Semantik auf ``decimal.Decimal`` ab, damit der erzeugte Code die Ausdrücke
unverändert übernehmen kann:

- ``add``/``subtract``/``multiply`` rechnen exakt.
- ``divide(x)`` ohne Skala muss exakt aufgehen, sonst Fehler (wie Java).
- ``divide(x, scale, mode)`` und ``setScale(scale, mode)`` runden auf die Skala.
- ``compareTo`` liefert -1, 0 oder 1.
- ``valueOf`` übernimmt ``double``-Literale über ihre kürzeste Dezimaldarstellung
  (wie ``Double.toString`` in Java).
"""

from __future__ import annotations

from decimal import (
    ROUND_DOWN as _DEC_ROUND_DOWN,
)
from decimal import (
    ROUND_HALF_UP as _DEC_ROUND_HALF_UP,
)
from decimal import (
    ROUND_UP as _DEC_ROUND_UP,
)
from decimal import (
    Context,
    Decimal,
    Inexact,
)

ROUND_UP = _DEC_ROUND_UP
ROUND_DOWN = _DEC_ROUND_DOWN
ROUND_HALF_UP = _DEC_ROUND_HALF_UP

_PRECISION = 60
_EXACT = Context(prec=_PRECISION, traps=[Inexact])
_WORK = Context(prec=_PRECISION)


class PapArithmeticError(ArithmeticError):
    """Division ohne Skala ging nicht exakt auf (Java: ArithmeticException)."""


class BD:
    """Unveränderliche Dezimalzahl mit der Schnittstelle von ``java.math.BigDecimal``."""

    __slots__ = ("value",)

    ZERO: BD
    ONE: BD
    TEN: BD
    ROUND_UP = ROUND_UP
    ROUND_DOWN = ROUND_DOWN
    ROUND_HALF_UP = ROUND_HALF_UP

    def __init__(self, value: object = 0) -> None:
        self.value = _to_decimal(value)

    @staticmethod
    def valueOf(value: object) -> BD:  # noqa: N802 - Java-Name aus dem PAP
        return value if isinstance(value, BD) else BD(value)

    def add(self, other: BD) -> BD:
        return BD(_WORK.add(self.value, _operand(other)))

    def subtract(self, other: BD) -> BD:
        return BD(_WORK.subtract(self.value, _operand(other)))

    def multiply(self, other: BD) -> BD:
        return BD(_WORK.multiply(self.value, _operand(other)))

    def divide(self, other: BD, scale: int | None = None, rounding: str | None = None) -> BD:
        divisor = _operand(other)
        if scale is None:
            try:
                return BD(_EXACT.divide(self.value, divisor))
            except Inexact as exc:
                raise PapArithmeticError(
                    f"Division {self.value} / {divisor} geht nicht exakt auf."
                ) from exc
        mode = rounding or ROUND_HALF_UP
        # Mit derselben Rundungsart vorrunden und dann auf die Skala: für
        # Ab- und Aufrunden ist das gleichwertig zur exakten Rundung.
        quotient = Context(prec=_PRECISION, rounding=mode).divide(self.value, divisor)
        return BD(quotient.quantize(Decimal(1).scaleb(-scale), rounding=mode))

    def setScale(self, scale: int, rounding: str | None = None) -> BD:  # noqa: N802
        mode = rounding or ROUND_HALF_UP
        return BD(self.value.quantize(Decimal(1).scaleb(-scale), rounding=mode))

    def compareTo(self, other: BD) -> int:  # noqa: N802
        result = self.value.compare(_operand(other))
        return int(result)

    def longValue(self) -> int:  # noqa: N802
        return int(self.value)

    def __repr__(self) -> str:
        return f"BD({self.value})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BD):
            return self.value == other.value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, BD):
        return value.value
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        return Decimal(value.strip() or "0")
    raise TypeError(f"Kein Dezimalwert: {value!r}")


def _operand(value: object) -> Decimal:
    if isinstance(value, BD):
        return value.value
    raise TypeError(f"BigDecimal-Operand erwartet, nicht {value!r}")


BD.ZERO = BD(0)
BD.ONE = BD(1)
BD.TEN = BD(10)
