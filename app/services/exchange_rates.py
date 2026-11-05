"""Fremdwährung: Wechselkurse, Umrechnung und Stichtagsbewertung offener Posten (ADR-010).

Die Buchhaltung läuft in der Firmenwährung. Fremdbetrag und Kurs stehen zusätzlich an der
Buchungszeile und am offenen Posten. Kurse folgen der Notierung der EZB: Einheiten der Fremdwährung
je 1 Einheit Firmenwährung, also ``Firmenwährung = Fremdbetrag / Kurs``. Weil die EZB Kurse je Euro
veröffentlicht, setzt Fremdwährung den Euro als Firmenwährung voraus.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    SOURCE_FX_VALUATION,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.standard_accounts import (
    KURSAUFWAND,
    KURSERTRAG,
    find_standard_account,
)
from domain.models import (
    Account,
    Company,
    ExchangeRate,
    JournalEntry,
    OpenItem,
    VatExchangeRate,
)

CENT = Decimal("0.01")
RATE_PLACES = Decimal("0.000001")
ZERO = Decimal("0.00")
BASE_CURRENCY = "EUR"
# An Wochenenden und Feiertagen veröffentlicht die EZB keine Kurse: Der letzte davor gilt, aber
# nicht unbegrenzt lange.
MAX_RATE_AGE_DAYS = 10
SOURCE_ECB = "ecb"
SOURCE_MANUAL = "manual"
SOURCE_BMF = "bmf"
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_TIMEOUT_SECONDS = 20
ECB_NAMESPACE = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
LONG_TERM_DAYS = 365
# Numeric(14, 6): acht Stellen vor dem Komma.
MAX_RATE = Decimal("100000000")
# Datenportal des BMF: eine CSV-Datei je Jahr, die laufende wird monatlich fortgeschrieben.
BMF_RATES_URL = (
    "https://www.bundesfinanzministerium.de/Datenportal/Daten/offene-daten/steuern-zoelle/"
    "umsatzsteuer-umrechnungskurse/datensaetze/uu-kurse-{year}-csv.csv?__blob=publicationFile"
)
BMF_TIMEOUT_SECONDS = 30
BMF_FIRST_YEAR = 2010
# Spaltenköpfe wechseln je Jahr („Januar[1]“, „Januar1“, „Jan 01“, „Mrz 03“).
BMF_MONTH_PREFIXES = {
    "jan": 1,
    "feb": 2,
    "mär": 3,
    "mrz": 3,
    "apr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dez": 12,
}
# Deutsche Schreibweise: „1,1738 USD“, „19.757,02 IDR“. In einzelnen Jahren fehlt ein Buchstabe.
BMF_RATE_CELL = re.compile(r"^(\d{1,3}(?:\.\d{3})+|\d+),(\d+)\s*([A-Z]{2,3})$")


class ExchangeRateError(ValueError):
    """Ungültiger Kurs, fehlender Kurs oder gescheiterter Abruf."""


class ExchangeRateSourceError(ExchangeRateError):
    """Die Kursquelle liefert nicht oder nichts Lesbares; am Aufruf liegt es nicht."""


def normalize_currency(value: Any) -> str:
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ExchangeRateError(f"Währungscode muss aus drei Buchstaben bestehen: {value!r}.")
    return code


def normalize_rate(value: Any) -> Decimal:
    try:
        rate = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError) as exc:
        raise ExchangeRateError(f"Ungültiger Kurs: {value!r}.") from exc
    if not rate.is_finite() or rate <= 0:
        raise ExchangeRateError("Der Kurs muss größer 0 sein.")
    if rate >= MAX_RATE:
        raise ExchangeRateError(f"Der Kurs {value} ist zu groß.")
    rate = rate.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)
    if rate <= 0:
        raise ExchangeRateError("Der Kurs muss größer 0 sein.")
    return rate


def to_company_amount(foreign_amount: Decimal, rate: Decimal) -> Decimal:
    """Fremdbetrag in Firmenwährung, kaufmännisch auf Cent gerundet."""
    return (foreign_amount / rate).quantize(CENT, rounding=ROUND_HALF_UP)


def require_base_currency(company: Company) -> None:
    if company.currency_code != BASE_CURRENCY:
        raise ExchangeRateError(
            f"Fremdwährung setzt {BASE_CURRENCY} als Firmenwährung voraus; "
            f"{company.name} führt {company.currency_code}."
        )


def find_exchange_rate(
    *, session: Session, tenant_id: int, currency_code: str, on: date
) -> ExchangeRate | None:
    """Letzter Kurs am Stichtag oder bis zu ``MAX_RATE_AGE_DAYS`` davor."""
    return (
        session.execute(
            select(ExchangeRate)
            .where(
                ExchangeRate.tenant_id == tenant_id,
                ExchangeRate.currency_code == currency_code,
                ExchangeRate.rate_date <= on,
                ExchangeRate.rate_date >= on - timedelta(days=MAX_RATE_AGE_DAYS),
            )
            .order_by(ExchangeRate.rate_date.desc())
        )
        .scalars()
        .first()
    )


def rate_for(*, session: Session, tenant_id: int, currency_code: str, on: date) -> Decimal:
    """Kurs am Stichtag; ein fehlender Kurs ist ein Fehler, nie ein stiller Standardwert."""
    row = find_exchange_rate(
        session=session, tenant_id=tenant_id, currency_code=currency_code, on=on
    )
    if row is None:
        raise ExchangeRateError(
            f"Für {currency_code} gibt es keinen Kurs zum {on.strftime('%d.%m.%Y')} "
            f"(auch nicht aus den {MAX_RATE_AGE_DAYS} Tagen davor). Kurs angeben oder unter "
            "Fremdwährung erfassen."
        )
    return row.rate


def serialize_exchange_rate(row: ExchangeRate) -> dict[str, Any]:
    return {
        "id": row.id,
        "currency_code": row.currency_code,
        "rate_date": row.rate_date.isoformat(),
        "rate": str(row.rate),
        "source": row.source,
        "created_by": row.created_by,
    }


def list_exchange_rates(
    *,
    session: Session,
    tenant_id: int,
    currency_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 200,
) -> list[ExchangeRate]:
    stmt = (
        select(ExchangeRate)
        .where(ExchangeRate.tenant_id == tenant_id)
        .order_by(ExchangeRate.rate_date.desc(), ExchangeRate.currency_code)
        .limit(max(1, min(int(limit), 1000)))
    )
    if currency_code:
        stmt = stmt.where(ExchangeRate.currency_code == normalize_currency(currency_code))
    if date_from is not None:
        stmt = stmt.where(ExchangeRate.rate_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(ExchangeRate.rate_date <= date_to)
    return list(session.execute(stmt).scalars())


def set_exchange_rate(
    *,
    session: Session,
    company: Company,
    currency_code: Any,
    rate_date: date,
    rate: Any,
    changed_by: str,
) -> ExchangeRate:
    """Legt einen Kurs an oder ändert ihn; bereits gebuchte Beträge bleiben unberührt."""
    require_base_currency(company)
    code = normalize_currency(currency_code)
    if code == company.currency_code:
        raise ExchangeRateError("Für die Firmenwährung gibt es keinen Kurs.")
    value = normalize_rate(rate)
    row = session.execute(
        select(ExchangeRate).where(
            ExchangeRate.tenant_id == company.tenant_id,
            ExchangeRate.currency_code == code,
            ExchangeRate.rate_date == rate_date,
        )
    ).scalar_one_or_none()
    before = str(row.rate) if row is not None else None
    if row is None:
        row = ExchangeRate(
            tenant_id=company.tenant_id,
            currency_code=code,
            rate_date=rate_date,
            rate=value,
            source=SOURCE_MANUAL,
            created_by=changed_by,
        )
        session.add(row)
    else:
        row.rate = value
        row.source = SOURCE_MANUAL
        row.created_by = changed_by
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="exchange_rate",
        entity_id=f"{code}:{rate_date.isoformat()}",
        action="set",
        changed_by=changed_by,
        payload={"before": before, "after": str(value), "rate_date": rate_date.isoformat()},
    )
    session.commit()
    session.refresh(row)
    return row


def parse_ecb_rates(content: bytes) -> list[tuple[date, str, Decimal]]:
    """Liest die EZB-Datei ``eurofxref``: Datum, Währung und Kurs je 1 Euro."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ExchangeRateError("Die Kursdatei der EZB ist nicht lesbar.") from exc
    rates: list[tuple[date, str, Decimal]] = []
    for day in root.iterfind(".//e:Cube[@time]", ECB_NAMESPACE):
        try:
            rate_date = date.fromisoformat(day.attrib["time"])
            for cube in day.iterfind("e:Cube[@currency]", ECB_NAMESPACE):
                rates.append(
                    (
                        rate_date,
                        normalize_currency(cube.attrib["currency"]),
                        normalize_rate(cube.attrib["rate"]),
                    )
                )
        except (KeyError, ValueError) as exc:
            raise ExchangeRateError("Die Kursdatei der EZB ist nicht lesbar.") from exc
    if not rates:
        raise ExchangeRateError("Die Kursdatei der EZB enthält keine Kurse.")
    return rates


def _download_ecb_rates() -> bytes:
    request = urllib.request.Request(ECB_RATES_URL, headers={"User-Agent": "OpenBuchhaltung"})
    try:
        with urllib.request.urlopen(request, timeout=ECB_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ExchangeRateError(
            f"Die Kurse der EZB konnten nicht abgerufen werden: {exc}"
        ) from exc


def fetch_ecb_rates(
    *,
    session: Session,
    company: Company,
    changed_by: str,
    download: Callable[[], bytes] = _download_ecb_rates,
) -> dict[str, Any]:
    """Übernimmt die Referenzkurse der letzten 90 Tage. Manuelle Kurse bleiben unangetastet."""
    require_base_currency(company)
    rates = parse_ecb_rates(download())
    existing = {
        (row.currency_code, row.rate_date)
        for row in session.execute(
            select(ExchangeRate).where(ExchangeRate.tenant_id == company.tenant_id)
        ).scalars()
    }
    created = 0
    for rate_date, currency_code, rate in rates:
        if (currency_code, rate_date) in existing:
            continue
        session.add(
            ExchangeRate(
                tenant_id=company.tenant_id,
                currency_code=currency_code,
                rate_date=rate_date,
                rate=rate,
                source=SOURCE_ECB,
                created_by=changed_by,
            )
        )
        created += 1
    latest = max(rate_date for rate_date, _, _ in rates)
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="exchange_rate",
        entity_id="ecb",
        action="fetched",
        changed_by=changed_by,
        payload={"created": created, "latest_rate_date": latest.isoformat()},
    )
    session.commit()
    return {"created": created, "skipped": len(rates) - created, "latest_rate_date": latest}


# ---------------------------------------------------------------------------
# Umsatzsteuer-Umrechnungskurse des BMF (§ 16 Abs. 6 UStG), je Monat
# ---------------------------------------------------------------------------


def normalize_rate_month(value: Any) -> date:
    """Monatserster zu ``JJJJ-MM``, einem Datum oder einem Tag dieses Monats."""
    if isinstance(value, date):
        return value.replace(day=1)
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(f"{raw}-01" if len(raw) == 7 else raw).replace(day=1)
    except ValueError as exc:
        raise ExchangeRateError(f"Ungültiger Monat: {value!r} (erwartet JJJJ-MM).") from exc


def format_rate_month(month: date) -> str:
    return month.strftime("%Y-%m")


def find_vat_rate(
    *, session: Session, tenant_id: int, currency_code: str, month: date
) -> VatExchangeRate | None:
    return session.execute(
        select(VatExchangeRate).where(
            VatExchangeRate.tenant_id == tenant_id,
            VatExchangeRate.currency_code == currency_code,
            VatExchangeRate.rate_month == month.replace(day=1),
        )
    ).scalar_one_or_none()


def serialize_vat_exchange_rate(row: VatExchangeRate) -> dict[str, Any]:
    return {
        "id": row.id,
        "currency_code": row.currency_code,
        "month": format_rate_month(row.rate_month),
        "rate": str(row.rate),
        "source": row.source,
        "created_by": row.created_by,
    }


def list_vat_exchange_rates(
    *,
    session: Session,
    tenant_id: int,
    currency_code: str | None = None,
    month_from: Any = None,
    month_to: Any = None,
    limit: int = 200,
) -> list[VatExchangeRate]:
    stmt = (
        select(VatExchangeRate)
        .where(VatExchangeRate.tenant_id == tenant_id)
        .order_by(VatExchangeRate.rate_month.desc(), VatExchangeRate.currency_code)
        .limit(max(1, min(int(limit), 1000)))
    )
    if currency_code:
        stmt = stmt.where(VatExchangeRate.currency_code == normalize_currency(currency_code))
    if month_from:
        stmt = stmt.where(VatExchangeRate.rate_month >= normalize_rate_month(month_from))
    if month_to:
        stmt = stmt.where(VatExchangeRate.rate_month <= normalize_rate_month(month_to))
    return list(session.execute(stmt).scalars())


def set_vat_exchange_rate(
    *,
    session: Session,
    company: Company,
    currency_code: Any,
    month: Any,
    rate: Any,
    changed_by: str,
) -> VatExchangeRate:
    """Erfasst einen Monatskurs von Hand; schon gemeldete Zahlungen behalten ihren Kurs."""
    require_base_currency(company)
    code = normalize_currency(currency_code)
    if code == company.currency_code:
        raise ExchangeRateError("Für die Firmenwährung gibt es keinen Kurs.")
    rate_month = normalize_rate_month(month)
    value = normalize_rate(rate)
    row = find_vat_rate(
        session=session, tenant_id=company.tenant_id, currency_code=code, month=rate_month
    )
    before = str(row.rate) if row is not None else None
    if row is None:
        row = VatExchangeRate(
            tenant_id=company.tenant_id,
            currency_code=code,
            rate_month=rate_month,
            rate=value,
            source=SOURCE_MANUAL,
            created_by=changed_by,
        )
        session.add(row)
    else:
        row.rate = value
        row.source = SOURCE_MANUAL
        row.created_by = changed_by
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="vat_exchange_rate",
        entity_id=f"{code}:{format_rate_month(rate_month)}",
        action="set",
        changed_by=changed_by,
        payload={"before": before, "after": str(value)},
    )
    session.commit()
    session.refresh(row)
    return row


def _bmf_rows(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    try:
        return list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))
    except csv.Error as exc:
        raise ExchangeRateSourceError("Die Kursdatei des BMF ist nicht lesbar.") from exc


def _bmf_month_columns(header: Iterable[str]) -> dict[int, int]:
    columns = {}
    for index, cell in enumerate(header):
        month = BMF_MONTH_PREFIXES.get(cell.strip().lower()[:3])
        if month is not None:
            columns[index] = month
    return columns


def parse_bmf_rates(content: bytes, *, year: int) -> tuple[list[tuple[date, str, Decimal]], int]:
    """Liest die Jahresdatei des BMF: Monat, Währung und Kurs je 1 Euro, dazu die unlesbaren Zellen.

    Die Datei hat eine Zeile je Land und eine Spalte je Monat; noch nicht bekannte Monate sind leer.
    Den Währungscode nennt jede Zelle. Eine Zelle mit einem anderen Code als der Rest ihrer Zeile
    (die Dateien 2021 und 2022 enthalten „RW“ statt „KRW“) wird nicht geraten, sondern gezählt.
    """
    rows = _bmf_rows(content)
    header_index = next(
        (index for index, row in enumerate(rows) if row and row[0].strip().lower() == "land"), None
    )
    if header_index is None:
        raise ExchangeRateSourceError("Die Kursdatei des BMF ist nicht lesbar.")
    columns = _bmf_month_columns(rows[header_index])
    if sorted(columns.values()) != list(range(1, 13)):
        raise ExchangeRateSourceError("Die Kursdatei des BMF nennt nicht alle zwölf Monate.")
    title = " ".join(cell for row in rows[:header_index] for cell in row)
    years = re.findall(r"\b(20\d\d)\b", title)
    if not years or int(years[0]) != year:
        raise ExchangeRateSourceError(f"Die Kursdatei des BMF gehört nicht zum Jahr {year}.")

    rates: list[tuple[date, str, Decimal]] = []
    ignored = 0
    for row in rows[header_index + 1 :]:
        if len(row) < 3 or not row[0].strip():
            continue
        parsed = []
        for index, month in columns.items():
            cell = " ".join(row[index].split()) if index < len(row) else ""
            if not cell:
                continue
            match = BMF_RATE_CELL.match(cell)
            if match is None:
                ignored += 1
                continue
            whole, fraction, code = match.groups()
            parsed.append((month, code, f"{whole.replace('.', '')}.{fraction}"))
        codes = Counter(code for _, code, _ in parsed if len(code) == 3)
        if not codes:
            ignored += len(parsed)
            continue
        currency = codes.most_common(1)[0][0]
        for month, code, value in parsed:
            if code != currency:
                ignored += 1
                continue
            try:
                rates.append(
                    (date(year, month, 1), normalize_currency(code), normalize_rate(value))
                )
            except ExchangeRateError:
                ignored += 1
    return rates, ignored


def _download_bmf_rates(year: int) -> bytes | None:
    """Jahresdatei des BMF; None, wenn es sie noch nicht gibt (Anfang Januar)."""
    request = urllib.request.Request(
        BMF_RATES_URL.format(year=year), headers={"User-Agent": "OpenBuchhaltung"}
    )
    try:
        with urllib.request.urlopen(request, timeout=BMF_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ExchangeRateSourceError(
            f"Die Kurse des BMF konnten nicht abgerufen werden: {exc}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ExchangeRateSourceError(
            f"Die Kurse des BMF konnten nicht abgerufen werden: {exc}"
        ) from exc


def fetch_bmf_rates(
    *,
    session: Session,
    company: Company,
    changed_by: str,
    year: int | None = None,
    download: Callable[[int], bytes | None] = _download_bmf_rates,
) -> dict[str, Any]:
    """Übernimmt die Monatskurse eines Jahres, ohne Angabe die des laufenden und des Vorjahres.

    Der Dezemberkurs erscheint erst im Januar, deshalb auch das Vorjahr. Manuelle Kurse bleiben
    unangetastet, eine Korrektur des BMF wird übernommen. Schon gemeldete Zahlungen behalten ihren
    Kurs, er steht am Ausgleich.
    """
    require_base_currency(company)
    current_year = date.today().year
    if year is not None and not BMF_FIRST_YEAR <= year <= current_year:
        raise ExchangeRateError(
            f"Das BMF stellt Kurse von {BMF_FIRST_YEAR} bis {current_year} bereit."
        )
    years = [year] if year is not None else [current_year - 1, current_year]

    rates: list[tuple[date, str, Decimal]] = []
    ignored = 0
    fetched_years = []
    for candidate in years:
        content = download(candidate)
        if content is None:
            continue
        parsed, not_readable = parse_bmf_rates(content, year=candidate)
        rates.extend(parsed)
        ignored += not_readable
        fetched_years.append(candidate)
    if not fetched_years:
        raise ExchangeRateSourceError(
            f"Beim BMF liegt keine Kursdatei für {', '.join(str(item) for item in years)} vor."
        )

    existing = {
        (row.currency_code, row.rate_month): row
        for row in session.execute(
            select(VatExchangeRate).where(
                VatExchangeRate.tenant_id == company.tenant_id,
                VatExchangeRate.rate_month >= date(min(fetched_years), 1, 1),
                VatExchangeRate.rate_month <= date(max(fetched_years), 12, 1),
            )
        ).scalars()
    }
    created = updated = 0
    for rate_month, currency_code, rate in rates:
        row = existing.get((currency_code, rate_month))
        if row is None:
            session.add(
                VatExchangeRate(
                    tenant_id=company.tenant_id,
                    currency_code=currency_code,
                    rate_month=rate_month,
                    rate=rate,
                    source=SOURCE_BMF,
                    created_by=changed_by,
                )
            )
            created += 1
        elif row.source == SOURCE_BMF and row.rate != rate:
            row.rate = rate
            row.created_by = changed_by
            updated += 1
    latest = max((rate_month for rate_month, _, _ in rates), default=None)
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="vat_exchange_rate",
        entity_id="bmf",
        action="fetched",
        changed_by=changed_by,
        payload={
            "years": fetched_years,
            "created": created,
            "updated": updated,
            "ignored": ignored,
            "latest_month": format_rate_month(latest) if latest else None,
        },
    )
    session.commit()
    return {
        "created": created,
        "updated": updated,
        "skipped": len(rates) - created - updated,
        "ignored": ignored,
        "years": fetched_years,
        "latest_month": latest,
    }


# ---------------------------------------------------------------------------
# Stichtagsbewertung offener Fremdwährungsposten (§ 256a HGB)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FxValuationRow:
    item: OpenItem
    # Stand zum Stichtag, nicht der heutige: Der Posten kann inzwischen bezahlt sein.
    foreign_open: Decimal
    carrying: Decimal
    rate: Decimal | None
    value: Decimal | None
    # Vorzeichen aus Sicht des Ergebnisses: positiv = Ertrag, negativ = Aufwand.
    difference: Decimal
    # Davon zu buchen: Bei mehr als einem Jahr Restlaufzeit nur der Aufwand (Realisationsprinzip).
    booked_difference: Decimal
    long_term: bool


def _open_as_of(session: Session, item: OpenItem, as_of: date) -> tuple[Decimal, Decimal]:
    """Offener Fremdbetrag und Buchwert zum Stichtag, aus den Ausgleichen zurückgerechnet.

    Ein Ausgleich zählt, wenn er bis zum Stichtag gezahlt und da noch nicht storniert war. Mit
    einer stornierten Rechnung ist der Posten ab dem Datum des Stornos weg.
    """
    if item.status == "settled" and item.settlement_journal_entry_id is not None:
        closing = session.get(JournalEntry, item.settlement_journal_entry_id)
        if (
            closing is not None
            and closing.reversal_of_id is not None
            and closing.entry_date <= as_of
        ):
            return ZERO, ZERO
    foreign_open = item.foreign_original_amount
    carrying = item.original_amount
    for settlement in item.settlements:
        if settlement.settlement_date > as_of:
            continue
        if settlement.reversed_on is not None and settlement.reversed_on <= as_of:
            continue
        foreign_open -= settlement.foreign_amount or ZERO
        carrying -= settlement.amount
    return foreign_open, carrying


def fx_valuation_rows(*, session: Session, company: Company, as_of: date) -> list[FxValuationRow]:
    """Zum Stichtag offene Fremdwährungsposten mit Wert zum Stichtagskurs."""
    items = session.execute(
        select(OpenItem)
        .options(
            selectinload(OpenItem.account),
            selectinload(OpenItem.partner),
            selectinload(OpenItem.settlements),
        )
        .where(
            OpenItem.company_id == company.id,
            OpenItem.foreign_currency_code.is_not(None),
            OpenItem.entry_date <= as_of,
        )
        .order_by(OpenItem.foreign_currency_code, OpenItem.due_date, OpenItem.id)
    ).scalars()
    rows = []
    for item in items:
        foreign_open, carrying = _open_as_of(session, item, as_of)
        if foreign_open <= 0:
            continue
        found = find_exchange_rate(
            session=session,
            tenant_id=company.tenant_id,
            currency_code=item.foreign_currency_code,
            on=as_of,
        )
        long_term = item.due_date is not None and (item.due_date - as_of).days > LONG_TERM_DAYS
        if found is None:
            rows.append(
                FxValuationRow(item, foreign_open, carrying, None, None, ZERO, ZERO, long_term)
            )
            continue
        value = to_company_amount(foreign_open, found.rate)
        difference = value - carrying
        if item.item_type == "payable":
            difference = -difference
        booked = min(difference, ZERO) if long_term else difference
        rows.append(
            FxValuationRow(
                item, foreign_open, carrying, found.rate, value, difference, booked, long_term
            )
        )
    return rows


def fx_valuation_report(*, session: Session, company: Company, as_of: date) -> dict[str, Any]:
    rows = fx_valuation_rows(session=session, company=company, as_of=as_of)
    existing = _existing_valuation(session, company.id, as_of)
    return {
        "company_id": company.id,
        "as_of": as_of,
        "rows": rows,
        "total_gain": sum(
            (row.booked_difference for row in rows if row.booked_difference > 0), ZERO
        ),
        "total_loss": sum(
            (row.booked_difference for row in rows if row.booked_difference < 0), ZERO
        ),
        "missing_rates": sorted(
            {row.item.foreign_currency_code for row in rows if row.rate is None}
        ),
        "posted_entry_id": existing.id if existing is not None else None,
        "posted_posting_number": existing.posting_number if existing is not None else None,
    }


def serialize_fx_valuation(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": report["company_id"],
        "as_of": report["as_of"].isoformat(),
        "rows": [
            {
                "open_item_id": row.item.id,
                "reference": row.item.reference,
                "item_type": row.item.item_type,
                "counterparty": row.item.counterparty,
                "currency_code": row.item.foreign_currency_code,
                "foreign_open_amount": str(row.foreign_open),
                "booked_rate": str(row.item.exchange_rate),
                "carrying_amount": str(row.carrying),
                "rate": str(row.rate) if row.rate is not None else None,
                "value": str(row.value) if row.value is not None else None,
                "difference": str(row.difference),
                "booked_difference": str(row.booked_difference),
                "long_term": row.long_term,
            }
            for row in report["rows"]
        ],
        "total_gain": str(report["total_gain"]),
        "total_loss": str(report["total_loss"]),
        "missing_rates": report["missing_rates"],
        "posted_entry_id": report["posted_entry_id"],
        "posted_posting_number": report["posted_posting_number"],
    }


def _existing_valuation(session: Session, company_id: int, as_of: date) -> JournalEntry | None:
    return (
        session.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.source == SOURCE_FX_VALUATION,
                JournalEntry.entry_date == as_of,
                JournalEntry.reversal_of_id.is_(None),
            )
        )
        .scalars()
        .first()
    )


def fx_account(session: Session, company_id: int, *, gain: bool) -> Account:
    """Konto für Kursgewinne bzw. -verluste; ohne dieses Konto wird nichts gebucht."""
    standard = KURSERTRAG if gain else KURSAUFWAND
    account = find_standard_account(session=session, company_id=company_id, standard=standard)
    if account is None:
        raise ExchangeRateError(
            f"Konto „{standard.label}“ fehlt ({' bzw. '.join(reversed(standard.codes))} in "
            "SKR03/SKR04). Bitte im Kontenplan anlegen."
        )
    return account


def post_fx_valuation(
    *, session: Session, company: Company, as_of: date, changed_by: str
) -> dict[str, Any]:
    """Bucht die Bewertung zum Stichtag und löst sie am Folgetag wieder auf.

    Der offene Posten behält seinen Buchwert, damit die Kursdifferenz bei der Zahlung gegen den
    ursprünglichen Kurs entsteht; deshalb die Auflösung, statt den Posten umzubewerten.
    """
    if _existing_valuation(session, company.id, as_of) is not None:
        raise ExchangeRateError(
            f"Die Bewertung zum {as_of.strftime('%d.%m.%Y')} ist bereits gebucht."
        )
    rows = fx_valuation_rows(session=session, company=company, as_of=as_of)
    missing = sorted({row.item.foreign_currency_code for row in rows if row.rate is None})
    if missing:
        raise ExchangeRateError(
            f"Zum {as_of.strftime('%d.%m.%Y')} fehlen Kurse für {', '.join(missing)}."
        )
    # Einzelbewertung: Gewinne und Verluste desselben Partners werden nicht verrechnet.
    per_target: dict[tuple[int, int | None, bool], Decimal] = {}
    for row in rows:
        if row.booked_difference == 0:
            continue
        # Ein Ertrag erhöht die Forderung (Soll) bzw. mindert die Verbindlichkeit (Soll).
        key = (row.item.account_id, row.item.partner_id, row.booked_difference > 0)
        per_target[key] = per_target.get(key, ZERO) + row.booked_difference
    if not per_target:
        raise ExchangeRateError("Es gibt keine Kursdifferenz zu buchen.")

    zero = Decimal("0.00")
    lines: list[JournalLineInput] = []
    gain_total = loss_total = zero
    for (account_id, partner_id, _gain), amount in per_target.items():
        lines.append(
            JournalLineInput(
                account_id=account_id,
                debit_amount=amount if amount > 0 else zero,
                credit_amount=-amount if amount < 0 else zero,
                partner_id=partner_id,
                description="Stichtagsbewertung Fremdwährung",
            )
        )
        if amount > 0:
            gain_total += amount
        else:
            loss_total += -amount
    if gain_total:
        lines.append(
            JournalLineInput(
                account_id=fx_account(session, company.id, gain=True).id,
                credit_amount=gain_total,
                description="Kursgewinn aus Stichtagsbewertung",
            )
        )
    if loss_total:
        lines.append(
            JournalLineInput(
                account_id=fx_account(session, company.id, gain=False).id,
                debit_amount=loss_total,
                description="Kursverlust aus Stichtagsbewertung",
            )
        )

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=as_of,
            description=f"Stichtagsbewertung Fremdwährung zum {as_of.strftime('%d.%m.%Y')}",
            status="posted",
            changed_by=changed_by,
            lines=lines,
            source=SOURCE_FX_VALUATION,
            expand_tax_lines=False,
        ),
        commit=False,
    )
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=as_of + timedelta(days=1),
        changed_by=changed_by,
        commit=False,
    )
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="fx_valuation",
        entity_id=as_of.isoformat(),
        action="posted",
        changed_by=changed_by,
        payload={
            "journal_entry_id": entry.id,
            "reversal_entry_id": reversal.id,
            "gain": str(gain_total),
            "loss": str(loss_total),
        },
    )
    session.commit()
    return {
        "journal_entry_id": entry.id,
        "posting_number": entry.posting_number,
        "reversal_entry_id": reversal.id,
        "reversal_posting_number": reversal.posting_number,
        "gain": gain_total,
        "loss": loss_total,
    }


def format_rate_text(company: Company, currency_code: str, rate: Decimal) -> str:
    """Kurs für Rechnung und Oberfläche, z. B. ``1 EUR = 1,0800 USD``."""
    text = f"{rate:.6f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"1 {company.currency_code} = {text} {currency_code}"
