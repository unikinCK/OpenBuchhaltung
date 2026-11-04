"""Fremdwährung: Wechselkurse, Umrechnung und Stichtagsbewertung offener Posten (ADR-010).

Die Buchhaltung läuft in der Firmenwährung. Fremdbetrag und Kurs stehen zusätzlich an der
Buchungszeile und am offenen Posten. Kurse folgen der Notierung der EZB: Einheiten der Fremdwährung
je 1 Einheit Firmenwährung, also ``Firmenwährung = Fremdbetrag / Kurs``. Weil die EZB Kurse je Euro
veröffentlicht, setzt Fremdwährung den Euro als Firmenwährung voraus.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
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
from domain.models import Account, Company, ExchangeRate, JournalEntry, OpenItem

CENT = Decimal("0.01")
RATE_PLACES = Decimal("0.000001")
ZERO = Decimal("0.00")
BASE_CURRENCY = "EUR"
# An Wochenenden und Feiertagen veröffentlicht die EZB keine Kurse: Der letzte davor gilt, aber
# nicht unbegrenzt lange.
MAX_RATE_AGE_DAYS = 10
SOURCE_ECB = "ecb"
SOURCE_MANUAL = "manual"
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_TIMEOUT_SECONDS = 20
ECB_NAMESPACE = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
LONG_TERM_DAYS = 365


class ExchangeRateError(ValueError):
    """Ungültiger Kurs, fehlender Kurs oder gescheiterter Abruf."""


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
# Stichtagsbewertung offener Fremdwährungsposten (§ 256a HGB)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FxValuationRow:
    item: OpenItem
    rate: Decimal | None
    value: Decimal | None
    # Vorzeichen aus Sicht des Ergebnisses: positiv = Ertrag, negativ = Aufwand.
    difference: Decimal
    # Davon zu buchen: Bei mehr als einem Jahr Restlaufzeit nur der Aufwand (Realisationsprinzip).
    booked_difference: Decimal
    long_term: bool


def fx_valuation_rows(*, session: Session, company: Company, as_of: date) -> list[FxValuationRow]:
    """Offene Fremdwährungsposten mit Wert zum Stichtagskurs."""
    items = session.execute(
        select(OpenItem)
        .options(selectinload(OpenItem.account), selectinload(OpenItem.partner))
        .where(
            OpenItem.company_id == company.id,
            OpenItem.status == "open",
            OpenItem.foreign_currency_code.is_not(None),
            OpenItem.entry_date <= as_of,
        )
        .order_by(OpenItem.foreign_currency_code, OpenItem.due_date, OpenItem.id)
    ).scalars()
    rows = []
    for item in items:
        found = find_exchange_rate(
            session=session,
            tenant_id=company.tenant_id,
            currency_code=item.foreign_currency_code,
            on=as_of,
        )
        long_term = item.due_date is not None and (item.due_date - as_of).days > LONG_TERM_DAYS
        if found is None:
            rows.append(FxValuationRow(item, None, None, ZERO, ZERO, long_term))
            continue
        value = to_company_amount(item.foreign_open_amount, found.rate)
        difference = value - item.open_amount
        if item.item_type == "payable":
            difference = -difference
        booked = min(difference, ZERO) if long_term else difference
        rows.append(FxValuationRow(item, found.rate, value, difference, booked, long_term))
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
                "foreign_open_amount": str(row.item.foreign_open_amount),
                "booked_rate": str(row.item.exchange_rate),
                "carrying_amount": str(row.item.open_amount),
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
    per_target: dict[tuple[int, int | None], Decimal] = {}
    for row in rows:
        if row.booked_difference == 0:
            continue
        # Ein Ertrag erhöht die Forderung (Soll) bzw. mindert die Verbindlichkeit (Soll).
        key = (row.item.account_id, row.item.partner_id)
        per_target[key] = per_target.get(key, ZERO) + row.booked_difference
    if not per_target:
        raise ExchangeRateError("Es gibt keine Kursdifferenz zu buchen.")

    zero = Decimal("0.00")
    lines: list[JournalLineInput] = []
    gain_total = loss_total = zero
    for (account_id, partner_id), amount in per_target.items():
        if amount == 0:
            continue
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
