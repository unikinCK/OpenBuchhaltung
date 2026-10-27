"""Anzeigeformate der Oberfläche: Beträge und Datumsangaben im deutschen Format.

Nur für die Darstellung – Formularwerte, CSV-Exporte und die API behalten das
maschinenlesbare Format (``1234.50``, ``2026-09-30``). Zeitpunkte werden in UTC
gespeichert und erst hier in deutsche Ortszeit (Europe/Berlin) umgerechnet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.web.blueprint import main_bp
from domain.formatting import format_amount, format_number, format_rate

# Zahlen: ``betrag`` 1.234,50 · ``satz`` 19 bzw. 10,7 · ``zahl`` 100.000
main_bp.add_app_template_filter(format_amount, "betrag")
main_bp.add_app_template_filter(format_rate, "satz")
main_bp.add_app_template_filter(format_number, "zahl")


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

