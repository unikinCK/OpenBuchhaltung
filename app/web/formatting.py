"""Anzeigeformate der Oberfläche: Beträge und Datumsangaben im deutschen Format.

Nur für die Darstellung – Formularwerte, CSV-Exporte und die API behalten das
maschinenlesbare Format (``1234.50``, ``2026-09-30``). Zeitpunkte werden in UTC
gespeichert und erst hier in deutsche Ortszeit (Europe/Berlin) umgerechnet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

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


DISPLAY_TIMEZONE = ZoneInfo("Europe/Berlin")


def _as_date(value: object) -> date | datetime | None:
    if isinstance(value, (date, datetime)):
        return value
    if isinstance(value, str) and value:
        try:
            if len(value) == 10:
                return date.fromisoformat(value)
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def to_local_time(value: datetime) -> datetime:
    """UTC-Zeitpunkt → deutsche Ortszeit; naive Werte (SQLite) gelten als UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(DISPLAY_TIMEZONE)


@main_bp.app_template_filter("datum")
def format_date(value: object) -> str:
    """``2026-09-30`` (auch als Datum/Zeitpunkt) → ``30.09.2026``.

    Zeitpunkte zählen zum Kalendertag der deutschen Ortszeit.
    """
    parsed = _as_date(value)
    if parsed is None:
        return "" if value is None else str(value)
    if isinstance(parsed, datetime):
        parsed = to_local_time(parsed)
    return parsed.strftime("%d.%m.%Y")


@main_bp.app_template_filter("datum_zeit")
def format_datetime(value: object, seconds: bool = False) -> str:
    """Zeitpunkt → ``30.09.2026 14:05`` in deutscher Ortszeit.

    Datumswerte ohne Uhrzeit erscheinen unverändert nur als Datum.
    """
    parsed = _as_date(value)
    if parsed is None:
        return "" if value is None else str(value)
    if not isinstance(parsed, datetime):
        return parsed.strftime("%d.%m.%Y")
    pattern = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return to_local_time(parsed).strftime(pattern)


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


@main_bp.app_template_filter("zahl")
def format_number(value: object) -> str:
    """Menge mit Tausenderpunkt, ohne überflüssige Nachkommastellen.

    ``100000.00`` → ``100.000``, ``1234.50`` → ``1.234,5``.
    """
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = format(number.normalize(), ",f")
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
