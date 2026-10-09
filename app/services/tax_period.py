"""Umsatzsteuerlicher Zeitpunkt einer Buchung aus Buchungs- und Leistungsdatum.

Ohne Leistungsdatum zählt das Buchungsdatum. Mit Leistungsdatum (Tag der
Lieferung oder sonstigen Leistung, bei Leistungszeiträumen deren Ende) gilt:

* ``SERVICE`` – Umsätze bei Sollversteuerung (§ 13 Abs. 1 Nr. 1 Buchst. a UStG),
  sonstige Leistungen an Unternehmer im übrigen Gemeinschaftsgebiet (§ 18b
  Satz 1 Nr. 2, § 18a Abs. 8 UStG) und § 13b-Leistungen eines EU-Unternehmers
  (§ 13b Abs. 1 UStG): der Zeitraum der Leistung.
* ``INVOICE`` – innergemeinschaftliche Lieferungen (§ 18b Satz 2, § 18a Abs. 8
  UStG), innergemeinschaftlicher Erwerb (§ 13 Abs. 1 Nr. 6 UStG) und die übrigen
  § 13b-Leistungen (§ 13b Abs. 2 UStG): Ausstellung der Rechnung (das
  Buchungsdatum), spätestens Ende des auf die Leistung folgenden Monats.
* ``INPUT`` – Vorsteuer (§ 15 Abs. 1 Satz 1 Nr. 1 UStG): Leistung erbracht und
  Rechnung vorhanden, also das spätere der beiden Daten.

Anzahlungen (Mindest-Istversteuerung) tragen kein Leistungsdatum; für sie
bleibt das Buchungsdatum maßgeblich.
"""

from __future__ import annotations

from datetime import date, timedelta

SERVICE = "service"
INVOICE = "invoice"
INPUT = "input"
TAX_POINT_RULES = (SERVICE, INVOICE, INPUT)

# Frühestes Leistungsdatum, dessen Steuer noch in einen Zeitraum ab ``date_from``
# fallen kann: ``INVOICE`` reicht bis zum Ende des Folgemonats (höchstens 62 Tage).
SERVICE_DATE_LOOKBACK = timedelta(days=62)


def end_of_following_month(day: date) -> date:
    """Letzter Tag des Monats nach ``day``."""
    first_of_next = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    first_after_next = date(
        first_of_next.year + first_of_next.month // 12, first_of_next.month % 12 + 1, 1
    )
    return first_after_next - timedelta(days=1)


def tax_point(entry_date: date, service_date: date | None, rule: str) -> date:
    """Datum, nach dem eine Buchung bzw. ein Teil davon dem Meldezeitraum zugeordnet wird."""
    if service_date is None:
        return entry_date
    if rule == SERVICE:
        return service_date
    if rule == INVOICE:
        return min(entry_date, end_of_following_month(service_date))
    if rule == INPUT:
        return max(entry_date, service_date)
    raise ValueError(f"Unbekannte Regel für den Steuerzeitpunkt: {rule}")
