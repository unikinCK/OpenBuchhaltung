"""Anzeigeformate der Oberfläche: Beträge und Datumsangaben im deutschen Format.

Nur für die Darstellung – Formularwerte, CSV-Exporte und die API behalten das
maschinenlesbare Format (``1234.50``, ``2026-09-30``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.web.blueprint import main_bp


@main_bp.app_template_filter("betrag")
def format_amount(value: object, places: int = 2) -> str:
    """``1234.5`` → ``1.234,50``; leere Werte bleiben leer."""
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = f"{number:,.{places}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _as_date(value: object) -> date | datetime | None:
    if isinstance(value, (date, datetime)):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


@main_bp.app_template_filter("datum")
def format_date(value: object) -> str:
    """``2026-09-30`` (auch als Datum/Zeitpunkt) → ``30.09.2026``."""
    parsed = _as_date(value)
    if parsed is None:
        return "" if value is None else str(value)
    return parsed.strftime("%d.%m.%Y")


@main_bp.app_template_filter("datum_zeit")
def format_datetime(value: object) -> str:
    """Zeitpunkt → ``30.09.2026 14:05`` (Datumswerte ohne Uhrzeit nur als Datum)."""
    parsed = _as_date(value)
    if parsed is None:
        return "" if value is None else str(value)
    if not isinstance(parsed, datetime):
        return parsed.strftime("%d.%m.%Y")
    return parsed.strftime("%d.%m.%Y %H:%M")


@main_bp.app_template_filter("satz")
def format_rate(value: object) -> str:
    """Steuersatz ohne überflüssige Nachkommastellen: ``19.00`` → ``19``, ``10.70`` → ``10,7``."""
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = format(number.normalize(), "f")
    return text.replace(".", ",")
