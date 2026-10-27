"""Deutsche Schreibweise für Zahlen in Oberfläche und Meldungstexten.

Nur für Text, den Menschen lesen – Formularwerte, CSV-Exporte und die API-Felder
behalten das maschinenlesbare Format (``1234.50``).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _german_separators(text: str) -> str:
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def format_amount(value: object, places: int = 2) -> str:
    """``1234.5`` → ``1.234,50``; leere Werte bleiben leer."""
    if value is None or value == "":
        return ""
    number = _as_decimal(value)
    if number is None:
        return str(value)
    return _german_separators(f"{number:,.{places}f}")


def format_rate(value: object) -> str:
    """Steuersatz ohne überflüssige Nachkommastellen: ``19.00`` → ``19``, ``10.70`` → ``10,7``."""
    if value is None or value == "":
        return ""
    number = _as_decimal(value)
    if number is None:
        return str(value)
    return format(number.normalize(), "f").replace(".", ",")


def format_number(value: object) -> str:
    """Menge mit Tausenderpunkt, ohne überflüssige Nachkommastellen.

    ``100000.00`` → ``100.000``, ``1234.50`` → ``1.234,5``.
    """
    if value is None or value == "":
        return ""
    number = _as_decimal(value)
    if number is None:
        return str(value)
    return _german_separators(format(number.normalize(), ",f"))
