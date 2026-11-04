"""Umsatzsteuer-Voranmeldung (UStVA): Kennziffern-Berechnung und Snapshots.

Die Berechnung leitet die UStVA-Kennziffern aus den Journaldaten ab. Grundlage
sind Buchungszeilen mit Steuercode:

* **Steuerzeilen** (Zeile liegt auf dem Steuerkonto des Steuercodes) liefern die
  gebuchte Umsatz- bzw. Vorsteuer.
* **Basiszeilen** (übrige Zeilen mit Steuercode) liefern die
  Bemessungsgrundlagen.

Buchungszeilen **ohne Steuercode** (importierte oder manuelle Buchungen) werden
datengetrieben ausgewertet: Zeilen auf Steuerkonten (``company_tax_accounts``:
Steuerkonten der Steuercodes, sonst die Standard-Steuerkonten 1571/1576/1771/1776
bzw. 1401/1406/3801/3806 – auch ganz ohne Steuercodes) zählen als
Umsatz-/Vorsteuerzeilen; Ertragszeilen derselben Buchung bilden die
Bemessungsgrundlage, deren Steuersatz aus dem Verhältnis USt/Bemessungsgrundlage
abgeleitet wird.

Erträge **ohne Umsatzsteuer** ordnet die DATEV-Kontenfunktion des Kontos zu
(``data/kontenrahmen/datev_kontenfunktionen.csv``): 8125/4125 → Kz 41,
8336/4336 → Kz 21, 8338/4338 → Kz 45, 8100/4100 → Kz 48 usw.; Zeilen mit dem
0-%-Steuercode ohne solche Kontenfunktion zählen in Kz 48. Erträge ohne Steuer
auf Konten ohne Funktion (z. B. Zinsen) bleiben außerhalb der UStVA und
erscheinen als Hinweis – bisher landeten sie pauschal in Kz 48 (Review F10).

**Innergemeinschaftlicher Erwerb** und **§ 13b UStG als Leistungsempfänger**
erkennt die UStVA an den Steuerkonten (``company_special_tax_accounts``, SKR03
1572/1574, 1577/1578, 1772/1774, 1785/1787; SKR04 1402/1404, 1407/1408,
3802/3804, 3835/3837): Vorsteuer in Kz 61 bzw. 67, die Umsatzsteuer mit der
Bemessungsgrundlage aus den Aufwands-/Anlagenzeilen derselben Buchung in Kz 89/93
bzw. Kz 46/47 (Leistung eines EU-Unternehmers: Konto mit EU-Funktion wie 3123 oder
Partner mit EU-USt-IdNr.) oder Kz 84/85.

Die Richtung ergibt sich aus ``TaxCode.kind``: ``output`` = Umsatzsteuer
(Ausgangsumsätze), ``input`` = Vorsteuer (Eingangsleistungen). Steuerfreie
Umsätze (Steuersatz 0 %) werden über Basiszeilen auf Erlöskonten erkannt.

Kennziffern (amtliches UStVA-Formular 2026; Bemessungsgrundlagen in vollen Euro):
* Kz 81/86 (und 35): Steuerpflichtige Umsätze 19 %/7 % (andere Sätze), Kz 87: 0 %
* Kz 41/44/42: Innergemeinschaftliche Lieferungen (Neufahrzeuge, Dreiecksgeschäfte)
* Kz 43: Steuerfreie Umsätze mit Vorsteuerabzug, Kz 48: ohne Vorsteuerabzug
* Kz 21: Nicht steuerbare sonstige Leistungen nach § 18b (EU), Kz 45: übrige
  nicht steuerbare Umsätze, Kz 60: § 13b-Umsätze als leistender Unternehmer
* Kz 89/93: Innergemeinschaftliche Erwerbe 19 %/7 %, Kz 46/47 und 84/85:
  § 13b-Leistungen (Bemessungsgrundlage/Steuer)
* Kz 66/61/67: Vorsteuer aus Rechnungen, ig. Erwerb, § 13b
* Kz 83: Verbleibende USt-Vorauszahlung bzw. Überschuss (gebuchte USt − VSt)

Den Meldezeitraum bestimmt das Buchungsdatum oder, falls angegeben, das
**Leistungsdatum** der Buchung (``app.services.tax_period``): Umsätze zählen im
Zeitraum der Leistung (Sollversteuerung, § 18b Satz 1 Nr. 2 UStG für Kz 21),
innergemeinschaftliche Lieferungen, ig. Erwerbe und § 13b-Leistungen nach
§ 13b Abs. 2 mit der Rechnung (spätestens Ende des Folgemonats der Leistung),
Vorsteuer zum späteren Datum aus Leistung und Rechnung. Eine Rechnung vom 1.7.
für Leistungen im Juni gehört so in den Juni.

Stornobuchungen neutralisieren sich automatisch, da mit Salden gerechnet wird.
Buchungen der Abschlussperiode (13) bleiben standardmäßig außen vor
(``include_closing_entries``); Ergebnis- und Saldovorträge des
Jahresabschlusses zählen nie als Umsatz.

**Istversteuerung** (§ 20 UStG, ``Company.vat_cash_basis_from``): Rechnungen mit Umsatzsteuer und
einem verknüpften offenen Posten zählen anteilig erst mit der Zahlung
(``OpenItemSettlement``), siehe ``_apply_cash_basis``. Vorsteuer, steuerfreie Umsätze und direkt
bezahlte Umsätze bleiben unverändert (ADR-009). Rechnungen in Fremdwährung zählen je Zahlung zum
Monatskurs des Geldeingangs (§ 16 Abs. 6 Satz 2 UStG, ADR-010), siehe ``_foreign_paid_entry``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.services.account_chart_check import detect_company_chart
from app.services.audit_log import log_audit_event
from app.services.datev_account_functions import datev_account_function
from app.services.exchange_rates import (
    ExchangeRateError,
    find_vat_rate,
    format_rate_month,
    fx_account,
    to_company_amount,
)
from app.services.journal_entries import (
    CARRYFORWARD_SOURCES,
    SOURCE_VAT_FX_DIFFERENCE,
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    first_open_posting_date,
)
from app.services.tax_codes import company_special_tax_accounts, company_tax_accounts
from app.services.tax_period import (
    INPUT,
    INVOICE,
    SERVICE,
    SERVICE_DATE_LOOKBACK,
    tax_point,
)
from domain.formatting import format_amount, format_rate
from domain.models import (
    SUBLEDGER_DEBTOR,
    TAX_KIND_INPUT,
    TAX_KIND_OUTPUT,
    Account,
    BusinessPartner,
    Company,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    OpenItemSettlement,
    Period,
    TaxCode,
    VatReturn,
)
from domain.services.journal_entry_validation import JournalEntryValidationError


class VatReturnError(ValueError):
    """Raised when a UStVA request cannot be fulfilled."""


@dataclass(slots=True)
class VatReturnRow:
    kennziffer: str
    label: str
    amount: Decimal


ZERO = Decimal("0.00")
VAT_RETURN_KIND_ADVANCE = "advance"
# Kennzahlen steuerfreier bzw. nicht steuerbarer Umsätze aus der Kontenfunktion.
TAX_FREE_KENNZAHLEN = frozenset({"41", "44", "42", "43", "48", "21", "45", "60", "87"})
# Innergemeinschaftliche Lieferungen: Meldezeitraum der Rechnung, spätestens der
# Folgemonat der Lieferung (§ 18b Satz 2 UStG); übrige Umsätze nach Leistung.
INVOICE_DATE_KENNZAHLEN = frozenset({"41", "44", "42"})
# Mitgliedstaaten nach Ländercode der USt-IdNr. (Griechenland EL, Nordirland XI).
EU_COUNTRY_CODES = frozenset(
    "AT BE BG CY CZ DE DK EE EL ES FI FR GR HR HU "
    "IE IT LT LU LV MT NL PL PT RO SE SI SK XI".split()
)
KENNZAHL_LABELS = {
    "81": "Steuerpflichtige Umsätze 19 % (Bemessungsgrundlage)",
    "86": "Steuerpflichtige Umsätze 7 % (Bemessungsgrundlage)",
    "87": "Umsätze zum Steuersatz 0 % nach § 12 Abs. 3 UStG",
    "41": "Innergemeinschaftliche Lieferungen an Abnehmer mit USt-IdNr.",
    "44": "Innergemeinschaftliche Lieferungen neuer Fahrzeuge an Abnehmer ohne USt-IdNr.",
    "42": "Lieferungen des ersten Abnehmers bei innergemeinschaftlichen Dreiecksgeschäften",
    "43": "Weitere steuerfreie Umsätze mit Vorsteuerabzug (z. B. Ausfuhrlieferungen)",
    "48": "Steuerfreie Umsätze ohne Vorsteuerabzug (Bemessungsgrundlage)",
    "21": "Nicht steuerbare sonstige Leistungen nach § 18b Satz 1 Nr. 2 UStG (EU)",
    "45": "Übrige nicht steuerbare Umsätze (Leistungsort nicht im Inland)",
    "60": "Umsätze, für die der Leistungsempfänger die Steuer nach § 13b UStG schuldet",
    "89": "Steuerpflichtige innergemeinschaftliche Erwerbe 19 % (Bemessungsgrundlage)",
    "93": "Steuerpflichtige innergemeinschaftliche Erwerbe 7 % (Bemessungsgrundlage)",
    "46": "Sonstige Leistungen eines EU-Unternehmers, § 13b Abs. 1 UStG (Bemessungsgrundlage)",
    "47": "Steuer zu Kz 46",
    "84": "Andere Leistungen nach § 13b UStG (Bemessungsgrundlage)",
    "85": "Steuer zu Kz 84",
    "USt": "Gebuchte Umsatzsteuer",
    "66": "Abziehbare Vorsteuerbeträge",
    "61": "Vorsteuerbeträge aus dem innergemeinschaftlichen Erwerb",
    "67": "Vorsteuerbeträge aus Leistungen nach § 13b UStG",
    "83": "Verbleibende Vorauszahlung / Überschuss",
}
VAT_RETURN_KIND_ANNUAL = "annual"


def vat_return_kind_from_label(period_label: str) -> str:
    """Ordnet ein kanonisches Periodenlabel fachlich ein."""
    return VAT_RETURN_KIND_ANNUAL if "-" not in period_label.strip() else VAT_RETURN_KIND_ADVANCE


def vat_return_display_name(period_label: str) -> str:
    return (
        "USt-Jahreserklärung"
        if vat_return_kind_from_label(period_label) == VAT_RETURN_KIND_ANNUAL
        else "UStVA"
    )


def period_bounds(period_label: str) -> tuple[date, date, str]:
    """Zeitraumgrenzen und kanonisches Label für einen Meldezeitraum.

    Unterstützte Formate (auch für andere Steuerarten wiederverwendbar):
    * "JJJJ-MM" — Monat
    * "JJJJ-Qn" — Quartal (Q1–Q4)
    * "JJJJ-Hn" — Halbjahr (H1–H2)
    * "JJJJ"    — Kalenderjahr
    """
    try:
        raw = period_label.strip().upper()
        if "-" not in raw:
            year = int(raw)
            start = date(year, 1, 1)
            end_month = 12
            canonical = f"{year}"
        else:
            year_raw, part = raw.split("-", 1)
            year = int(year_raw)
            if part.startswith("Q"):
                quarter = int(part[1:])
                if quarter not in {1, 2, 3, 4}:
                    raise ValueError
                start = date(year, 3 * quarter - 2, 1)
                end_month = 3 * quarter
                canonical = f"{year}-Q{quarter}"
            elif part.startswith("H"):
                half = int(part[1:])
                if half not in {1, 2}:
                    raise ValueError
                start = date(year, 6 * half - 5, 1)
                end_month = 6 * half
                canonical = f"{year}-H{half}"
            else:
                month = int(part)
                if month not in range(1, 13):
                    raise ValueError
                start = date(year, month, 1)
                end_month = month
                canonical = f"{year}-{month:02d}"
        if end_month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, end_month + 1, 1) - timedelta(days=1)
        return start, end, canonical
    except (ValueError, IndexError):
        raise VatReturnError(
            f"Ungültiger Meldezeitraum {period_label!r} "
            "(erwartet JJJJ-MM, JJJJ-Qn, JJJJ-Hn oder JJJJ)."
        ) from None


def _company_vat_accounts(session: Session, company_id: int) -> dict[int, str]:
    """Steuerkonten der Gesellschaft als ``{account_id: kind}`` (``input``/``output``).

    Über diese Zuordnung (``company_tax_accounts``: Steuercodes und
    Standard-Steuerkonten) werden auch Buchungszeilen ohne Steuercode als
    Umsatz-/Vorsteuerzeilen erkannt.
    """
    return {
        account_id: tax_account.kind
        for account_id, tax_account in company_tax_accounts(
            session=session, company_id=company_id
        ).items()
    }


def _match_rate(raw_rate: Decimal, known_rates: set[Decimal]) -> Decimal:
    """Ordnet einen rechnerisch abgeleiteten Steuersatz dem nächsten bekannten zu."""
    best = min(known_rates, key=lambda rate: abs(rate - raw_rate), default=None)
    if best is not None and abs(best - raw_rate) <= Decimal("1.0"):
        return best
    return raw_rate.quantize(Decimal("0.01"))


def revenue_kennzahl(chart: str | None, account_code: str) -> str | None:
    """Kennzahl eines steuerfreien bzw. nicht steuerbaren Umsatzes laut Kontenfunktion."""
    kennzahl = datev_account_function(chart, account_code).kennzahl
    return kennzahl if kennzahl in TAX_FREE_KENNZAHLEN else None


def partner_vat_country(country_code: str | None, vat_id: str | None) -> str | None:
    """Ländercode der USt-IdNr. (Präfix), sonst das Land des Partners."""
    vat = (vat_id or "").replace(" ", "").upper()
    if len(vat) > 2 and vat[:2].isalpha():
        return vat[:2]
    return (country_code or "").strip().upper() or None


@dataclass(slots=True)
class ForeignPaymentEvent:
    """Eine Zahlung auf eine Fremdwährungsrechnung oder ihr Storno im Meldezeitraum."""

    settlement: OpenItemSettlement
    reference: str
    is_reversal: bool
    currency_code: str
    # Monat des Geldeingangs, nach ihm richtet sich der Kurs.
    rate_month: date
    rate: Decimal
    # Kein Monatskurs bekannt: Dann steht hier vorläufig der Kurs der Rechnung.
    rate_missing: bool
    # Vorzeichenrichtig, das Storno negativ.
    foreign_amount: Decimal
    tax: Decimal
    booked_tax: Decimal
    # Steuerkonto → Differenz „gebucht minus gemeldet“; das Storno kehrt sie um.
    differences: dict[int, Decimal] = field(default_factory=dict)

    @property
    def is_posted(self) -> bool:
        settlement = self.settlement
        entry_id = (
            settlement.vat_fx_reversal_entry_id if self.is_reversal else settlement.vat_fx_entry_id
        )
        return entry_id is not None


def serialize_foreign_payment(event: ForeignPaymentEvent) -> dict[str, Any]:
    return {
        "reference": event.reference,
        "kind": "reversal" if event.is_reversal else "payment",
        "currency_code": event.currency_code,
        "month": format_rate_month(event.rate_month),
        "rate": str(event.rate),
        "rate_missing": event.rate_missing,
        "foreign_amount": str(event.foreign_amount),
        "tax": str(event.tax),
        "booked_tax": str(event.booked_tax),
        "difference": str(event.booked_tax - event.tax),
        "posted": event.is_posted,
    }


@dataclass(slots=True)
class VatReturnResult:
    """UStVA-Kennzahlen mit Hinweisen und nicht zugeordneten Erträgen."""

    rows: list[VatReturnRow]
    warnings: list[str] = field(default_factory=list)
    # Erträge ohne Umsatzsteuer auf Konten ohne UStVA-Funktion (nicht gemeldet).
    unassigned: list[dict[str, object]] = field(default_factory=list)
    foreign_payments: list[ForeignPaymentEvent] = field(default_factory=list)


@dataclass(slots=True)
class _Entry:
    posting_number: str
    entry_date: date
    service_date: date | None = None
    rows: list = field(default_factory=list)
    # Bei Stornobuchungen: ID der stornierten Originalbuchung.
    reversal_of_id: int | None = None

    def tax_point(self, rule: str) -> date:
        return tax_point(self.entry_date, self.service_date, rule)


def revenue_tax_rule(kennzahl: str | None) -> str:
    """Regel für den Steuerzeitpunkt eines Umsatzes mit dieser Kennzahl."""
    return INVOICE if kennzahl in INVOICE_DATE_KENNZAHLEN else SERVICE


def vat_entries(
    session: Session,
    company_id: int,
    date_from: date,
    date_to: date,
    include_closing_entries: bool,
) -> dict[int, _Entry]:
    """Buchungszeilen je Buchung (ohne Saldovorträge), deren Steuer in den Zeitraum
    fallen kann: Buchungsdatum im Zeitraum oder Leistungsdatum im Zeitraum bzw. bis
    zu zwei Monate davor. Welche Teile einer Buchung zählen, entscheidet der
    Steuerzeitpunkt (``_Entry.tax_point``). Die Differenzbuchung zum Monatskurs bildet die
    gemeldete Steuer im Hauptbuch nach und ist selbst kein Umsatz."""
    stmt = _vat_lines_select().where(
        JournalEntry.company_id == company_id,
        or_(
            and_(JournalEntry.entry_date >= date_from, JournalEntry.entry_date <= date_to),
            and_(
                JournalEntry.service_date >= date_from - SERVICE_DATE_LOOKBACK,
                JournalEntry.service_date <= date_to,
            ),
        ),
        JournalEntry.source.notin_((*CARRYFORWARD_SOURCES, SOURCE_VAT_FX_DIFFERENCE)),
    )
    if not include_closing_entries:
        stmt = stmt.where(Period.is_closing.is_(False))
    return _collect_entries(session.execute(stmt).all())


def _vat_lines_select():
    return (
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntry.posting_number,
            JournalEntry.entry_date,
            JournalEntry.service_date,
            JournalEntry.reversal_of_id,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.account_id,
            JournalEntryLine.partner_id,
            JournalEntryLine.tax_code_id,
            JournalEntryLine.foreign_currency_code,
            JournalEntryLine.foreign_amount,
            TaxCode.rate,
            TaxCode.vat_account_id,
            TaxCode.kind.label("tax_kind"),
            Account.code.label("account_code"),
            Account.name.label("account_name"),
            Account.account_type.label("line_account_type"),
            Account.subledger.label("account_subledger"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Period, Period.id == JournalEntry.period_id)
        .outerjoin(TaxCode, TaxCode.id == JournalEntryLine.tax_code_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .order_by(JournalEntry.entry_date, JournalEntry.id, JournalEntryLine.line_number)
    )


def _collect_entries(rows) -> dict[Any, _Entry]:
    entries: dict[Any, _Entry] = {}
    for row in rows:
        entry = entries.setdefault(
            row.journal_entry_id,
            _Entry(
                row.posting_number,
                row.entry_date,
                row.service_date,
                reversal_of_id=row.reversal_of_id,
            ),
        )
        entry.rows.append(row)
    return entries


def _output_tax_of_row(row, vat_accounts: dict[int, str]) -> Decimal:
    """Umsatzsteuer einer Zeile (Habensaldo); null für alle anderen Zeilen."""
    if row.tax_code_id is not None:
        is_output_tax = (
            row.vat_account_id is not None
            and row.account_id == row.vat_account_id
            and row.tax_kind != TAX_KIND_INPUT
        )
    else:
        is_output_tax = vat_accounts.get(row.account_id) == TAX_KIND_OUTPUT
    return row.credit_amount - row.debit_amount if is_output_tax else ZERO


def company_on_cash_basis(company: Company, day: date) -> bool:
    """Ob die Gesellschaft an diesem Tag nach vereinnahmten Entgelten versteuert."""
    if company.vat_cash_basis_from is None or day < company.vat_cash_basis_from:
        return False
    return company.vat_cash_basis_until is None or day <= company.vat_cash_basis_until


def _paid_share(events: list[tuple[date, Decimal]], total: Decimal, up_to: date) -> Decimal:
    """Bezahlter Anteil einer Rechnung zum Datum, begrenzt auf 0 bis 1."""
    paid = sum((amount for day, amount in events if day <= up_to), ZERO)
    return max(Decimal(0), min(Decimal(1), paid / total)) if total > ZERO else Decimal(0)


def _scaled(amount: Decimal, share: Decimal) -> Decimal:
    return (amount * share).quantize(ZERO, rounding=ROUND_HALF_UP)


def _row_foreign_amount(amount: Decimal, row, currency_code: str, invoice_rate: Decimal) -> Decimal:
    """Fremdbetrag einer Rechnungszeile: gebucht oder aus dem Eurobetrag zum Rechnungskurs."""
    if amount == ZERO:
        return ZERO
    if row.foreign_currency_code == currency_code and row.foreign_amount is not None:
        return row.foreign_amount
    return (amount * invoice_rate).quantize(ZERO, rounding=ROUND_HALF_UP)


def _foreign_paid_entry(
    *,
    session: Session,
    company: Company,
    invoice: _Entry,
    settlements: list[OpenItemSettlement],
    references: dict[int, str],
    currency_code: str,
    invoice_rate: Decimal,
    foreign_total: Decimal,
    date_from: date,
    date_to: date,
    vat_accounts: dict[int, str],
) -> tuple[_Entry | None, list[ForeignPaymentEvent]]:
    """Die Zeilen einer Fremdwährungsrechnung, soweit im Zeitraum bezahlt, zum Monatskurs.

    Jede Zahlung deckt einen festen Teil der Fremdbeträge der Rechnung und wird zum Kurs des Monats
    umgerechnet, in dem das Geld einging (§ 16 Abs. 6 Satz 2 UStG). Ihr Storno nimmt im Zeitraum des
    Stornos genau diesen Betrag zurück, auch wenn dort ein anderer Kurs gilt. Der Teil einer Zahlung
    ist der kumulierte Anteil abzüglich der Teile, die beim Eingang schon standen; so summieren sich
    die Teile ohne Rundungsrest zur Rechnung, und ein Storno verschiebt keinen späteren Teil.
    """
    ordered = sorted(settlements, key=lambda settlement: settlement.id)
    reversal_times = dict(
        session.execute(
            select(JournalEntry.id, JournalEntry.created_at).where(
                JournalEntry.id.in_(
                    [
                        settlement.reversal_journal_entry_id
                        for settlement in ordered
                        if settlement.reversal_journal_entry_id is not None
                    ]
                )
            )
        ).all()
    )
    totals: dict[tuple[int, str], Decimal] = {}
    events: list[ForeignPaymentEvent] = []
    foreign_parts: dict[int, dict[tuple[int, str], Decimal]] = {}
    booked_parts: dict[int, dict[tuple[int, str], Decimal]] = {}
    for settlement in ordered:
        # Was tatsächlich stand, nicht was nach den Daten stehen müsste: Das Datum einer Zahlung
        # kann vor dem Storno einer anderen liegen, die beim Erfassen schon weg war.
        standing = [
            other
            for other in ordered
            if other.id < settlement.id
            and (
                other.reversal_journal_entry_id is None
                or reversal_times[other.reversal_journal_entry_id] > settlement.created_at
            )
        ]
        foreign_paid = settlement.foreign_amount or ZERO
        share = min(
            Decimal(1),
            (sum((other.foreign_amount or ZERO for other in standing), ZERO) + foreign_paid)
            / foreign_total,
        )
        foreign_part: dict[tuple[int, str], Decimal] = {}
        booked_part: dict[tuple[int, str], Decimal] = {}
        for index, row in enumerate(invoice.rows):
            for column in ("debit_amount", "credit_amount"):
                key = (index, column)
                booked = getattr(row, column)
                foreign = _row_foreign_amount(booked, row, currency_code, invoice_rate)
                foreign_part[key] = _scaled(foreign, share) - sum(
                    (foreign_parts[other.id][key] for other in standing), ZERO
                )
                booked_part[key] = _scaled(booked, share) - sum(
                    (booked_parts[other.id][key] for other in standing), ZERO
                )
        foreign_parts[settlement.id] = foreign_part
        booked_parts[settlement.id] = booked_part

        reversal_in_period = (
            settlement.reversed_on is not None and date_from <= settlement.reversed_on <= date_to
        )
        payment_in_period = date_from <= settlement.settlement_date <= date_to
        if not payment_in_period and not reversal_in_period:
            continue

        rate_month = (settlement.received_on or settlement.settlement_date).replace(day=1)
        rate = settlement.vat_rate
        if rate is None:
            found = find_vat_rate(
                session=session,
                tenant_id=company.tenant_id,
                currency_code=currency_code,
                month=rate_month,
            )
            rate = found.rate if found is not None else None
        rate_missing = rate is None
        if rate is None:
            rate = invoice_rate

        amounts = {key: to_company_amount(part, rate) for key, part in foreign_part.items()}
        tax = booked_tax = ZERO
        differences: dict[int, Decimal] = {}
        for index, row in enumerate(invoice.rows):
            if _output_tax_of_row(row, vat_accounts) == ZERO:
                continue
            for column, sign in (("credit_amount", 1), ("debit_amount", -1)):
                amount = amounts[(index, column)]
                booked_slice = booked_part[(index, column)]
                tax += sign * amount
                booked_tax += sign * booked_slice
                differences[row.account_id] = (
                    differences.get(row.account_id, ZERO) + sign * (booked_slice - amount)
                )

        for is_reversal, in_period in ((False, payment_in_period), (True, reversal_in_period)):
            if not in_period:
                continue
            sign = -1 if is_reversal else 1
            for key, amount in amounts.items():
                totals[key] = totals.get(key, ZERO) + sign * amount
            events.append(
                ForeignPaymentEvent(
                    settlement=settlement,
                    reference=references[settlement.open_item_id],
                    is_reversal=is_reversal,
                    currency_code=currency_code,
                    rate_month=rate_month,
                    rate=rate,
                    rate_missing=rate_missing,
                    foreign_amount=sign * foreign_paid,
                    tax=sign * tax,
                    booked_tax=sign * booked_tax,
                    differences={
                        account_id: sign * difference
                        for account_id, difference in differences.items()
                    },
                )
            )
    if not events:
        return None, []
    paid = _Entry(invoice.posting_number, date_to)
    for index, row in enumerate(invoice.rows):
        values = row._asdict()
        for column in ("debit_amount", "credit_amount"):
            values[column] = totals.get((index, column), ZERO)
        paid.rows.append(SimpleNamespace(**values))
    return paid, events


def _apply_cash_basis(
    *,
    session: Session,
    company: Company,
    entries: dict[Any, _Entry],
    date_from: date,
    date_to: date,
    vat_accounts: dict[int, str],
) -> tuple[dict[Any, _Entry], list[str], list[ForeignPaymentEvent]]:
    """Ersetzt Rechnungen auf offene Posten durch den im Zeitraum bezahlten Anteil.

    Eine Rechnung mit Umsatzsteuer und verknüpftem Posten, gebucht im Zeitraum der
    Istversteuerung, zählt nicht mit dem Buchungsdatum. Stattdessen entsteht je Rechnung ein
    Eintrag mit ihren Zeilen, skaliert auf den Anteil, der im Meldezeitraum gezahlt wurde; ein
    storniertes Ausgleichen mindert im Zeitraum des Stornos. Gerechnet wird über den kumulierten
    Anteil, damit sich Teilzahlungen ohne Rundungsrest zur Rechnung summieren. Der Rest wird
    wie bisher ausgewertet, also auch die Vorsteuer und steuerfreie Umsätze.
    """
    filters = [
        OpenItem.company_id == company.id,
        OpenItem.item_type == "receivable",
        JournalEntry.entry_date >= company.vat_cash_basis_from,
    ]
    if company.vat_cash_basis_until is not None:
        filters.append(JournalEntry.entry_date <= company.vat_cash_basis_until)
    item_rows = session.execute(
        select(
            OpenItem.id,
            OpenItem.journal_entry_id,
            OpenItem.original_amount,
            OpenItem.foreign_currency_code,
            OpenItem.foreign_original_amount,
            OpenItem.exchange_rate,
            OpenItem.reference,
        )
        .join(JournalEntry, JournalEntry.id == OpenItem.journal_entry_id)
        .where(*filters)
    ).all()

    items_by_entry: dict[int, dict[int, Decimal]] = {}
    foreign_kinds: dict[int, set[tuple[str | None, Decimal | None]]] = {}
    foreign_totals: dict[int, Decimal] = {}
    references: dict[int, str] = {}
    for (
        item_id,
        entry_id,
        original_amount,
        foreign_currency_code,
        foreign_original_amount,
        exchange_rate,
        reference,
    ) in item_rows:
        items_by_entry.setdefault(entry_id, {})[item_id] = original_amount
        references[item_id] = reference
        foreign_kinds.setdefault(entry_id, set()).add((foreign_currency_code, exchange_rate))
        if foreign_currency_code:
            foreign_totals[entry_id] = foreign_totals.get(entry_id, ZERO) + (
                foreign_original_amount or ZERO
            )

    invoices = (
        _collect_entries(
            session.execute(
                _vat_lines_select().where(JournalEntry.id.in_(items_by_entry))
            ).all()
        )
        if items_by_entry
        else {}
    )
    deferred = {
        entry_id: invoice
        for entry_id, invoice in invoices.items()
        if any(_output_tax_of_row(row, vat_accounts) != ZERO for row in invoice.rows)
    }

    events_by_item: dict[int, list[tuple[date, Decimal]]] = {}
    settlements_by_item: dict[int, list[OpenItemSettlement]] = {}
    item_ids = [item_id for entry_id in deferred for item_id in items_by_entry[entry_id]]
    if item_ids:
        for settlement in session.execute(
            select(OpenItemSettlement).where(OpenItemSettlement.open_item_id.in_(item_ids))
        ).scalars():
            settlements_by_item.setdefault(settlement.open_item_id, []).append(settlement)
            events = events_by_item.setdefault(settlement.open_item_id, [])
            events.append((settlement.settlement_date, settlement.amount))
            if settlement.reversed_on is not None:
                events.append((settlement.reversed_on, -settlement.amount))

    # Bis zum Periodenende stornierte Rechnungen werden nicht mehr fällig.
    cancelled = (
        set(
            session.execute(
                select(JournalEntry.reversal_of_id).where(
                    JournalEntry.reversal_of_id.in_(deferred),
                    JournalEntry.entry_date <= date_to,
                )
            ).scalars()
        )
        if deferred
        else set()
    )

    # Stornos solcher Rechnungen entfallen wie die Rechnung selbst.
    result = {
        key: entry
        for key, entry in entries.items()
        if key not in deferred and entry.reversal_of_id not in deferred
    }

    not_due = ZERO
    mixed_foreign_paid: list[str] = []
    foreign_events: list[ForeignPaymentEvent] = []
    before = date_from - timedelta(days=1)
    for entry_id, invoice in deferred.items():
        item_amounts = items_by_entry[entry_id]
        total = sum(item_amounts.values(), ZERO)
        events = [
            event for item_id in item_amounts for event in events_by_item.get(item_id, [])
        ]
        share_before = _paid_share(events, total, before)
        share_after = _paid_share(events, total, date_to)
        if invoice.entry_date <= date_to and entry_id not in cancelled:
            not_due += sum(
                (
                    _output_tax_of_row(row, vat_accounts)
                    - _scaled(_output_tax_of_row(row, vat_accounts), share_after)
                    for row in invoice.rows
                ),
                ZERO,
            )
        kinds = foreign_kinds[entry_id]
        foreign_kind = next(iter(kinds))
        if len(kinds) == 1 and foreign_kind[0] and foreign_kind[1] and foreign_totals[entry_id]:
            foreign_entry, entry_events = _foreign_paid_entry(
                session=session,
                company=company,
                invoice=invoice,
                settlements=[
                    settlement
                    for item_id in item_amounts
                    for settlement in settlements_by_item.get(item_id, [])
                ],
                references=references,
                currency_code=foreign_kind[0],
                invoice_rate=foreign_kind[1],
                foreign_total=foreign_totals[entry_id],
                date_from=date_from,
                date_to=date_to,
                vat_accounts=vat_accounts,
            )
            foreign_events.extend(entry_events)
            if foreign_entry is not None:
                result[f"cash-basis-{entry_id}"] = foreign_entry
            continue
        if share_after == share_before:
            continue
        if any(code for code, _ in kinds):
            mixed_foreign_paid.append(invoice.posting_number)
        paid = _Entry(invoice.posting_number, date_to)
        for row in invoice.rows:
            values = row._asdict()
            for column in ("debit_amount", "credit_amount"):
                values[column] = _scaled(values[column], share_after) - _scaled(
                    values[column], share_before
                )
            paid.rows.append(SimpleNamespace(**values))
        result[f"cash-basis-{entry_id}"] = paid

    warnings: list[str] = []
    overlaps = date_to >= company.vat_cash_basis_from and (
        company.vat_cash_basis_until is None or date_from <= company.vat_cash_basis_until
    )
    if overlaps:
        warnings.append(
            "Istversteuerung: Rechnungen auf offene Posten zählen anteilig mit der Zahlung, "
            "die Vorsteuer und steuerfreie Umsätze unverändert."
        )
    if not_due != ZERO:
        warnings.append(
            f"Istversteuerung: {format_amount(not_due)} € Umsatzsteuer aus bis zum "
            f"{date_to.strftime('%d.%m.%Y')} gebuchten Rechnungen sind noch nicht fällig. "
            "Das Steuerkonto enthält sie bereits, deshalb weicht sein Saldo von der Zahllast ab."
        )
    if mixed_foreign_paid:
        warnings.append(
            "Istversteuerung: Auf diesen Rechnungen liegen Posten in verschiedenen Währungen oder "
            "zu verschiedenen Kursen. Sie zählen hier zum Kurs der Rechnung, nicht zum Monatskurs "
            "des Zahlungsmonats; bitte die Steuer prüfen und korrigieren: "
            + ", ".join(sorted(mixed_foreign_paid)[:10])
            + (" …" if len(mixed_foreign_paid) > 10 else "")
        )
    missing_rates = sorted(
        {(event.currency_code, event.rate_month) for event in foreign_events if event.rate_missing}
    )
    if missing_rates:
        warnings.append(
            "Istversteuerung: Es fehlt der Monatskurs für "
            + ", ".join(f"{code} {format_rate_month(month)}" for code, month in missing_rates)
            + ". Diese Zahlungen zählen vorläufig zum Kurs der Rechnung. Festhalten lässt sich der "
            "Zeitraum erst mit Kurs: unter Fremdwährung abrufen oder erfassen."
        )
    open_difference = sum(
        (
            event.booked_tax - event.tax
            for event in foreign_events
            if not event.is_posted and not event.rate_missing
        ),
        ZERO,
    )
    if open_difference != ZERO:
        direction = "niedriger" if open_difference > ZERO else "höher"
        warnings.append(
            "Istversteuerung: Rechnungen in Fremdwährung zählen zum Monatskurs des Zahlungsmonats "
            f"(§ 16 Abs. 6 Satz 2 UStG). Die Umsatzsteuer ist dadurch "
            f"{format_amount(abs(open_difference))} € {direction} als zum Kurs der Rechnung "
            "gebucht. Beim Festhalten wird das Steuerkonto gegen die Kursdifferenz angeglichen."
        )
    # Die Steuer entsteht mit der Zahlung; wird sie nach der Meldung zugeordnet, fehlt sie dort.
    late_payments = session.execute(
        select(OpenItem.reference, VatReturn.period_label)
        .join(OpenItemSettlement, OpenItemSettlement.open_item_id == OpenItem.id)
        .join(
            VatReturn,
            and_(
                VatReturn.company_id == company.id,
                VatReturn.date_from <= OpenItemSettlement.settlement_date,
                VatReturn.date_to >= OpenItemSettlement.settlement_date,
                VatReturn.created_at < OpenItemSettlement.created_at,
            ),
        )
        .where(
            OpenItem.id.in_(item_ids),
            OpenItemSettlement.reversed_on.is_(None),
            # Nur im betroffenen Zeitraum und in dem der Zuordnung: Eine gespeicherte Meldung lässt
            # sich hier nicht ersetzen, sonst stünde der Hinweis für immer da.
            or_(
                and_(
                    OpenItemSettlement.settlement_date >= date_from,
                    OpenItemSettlement.settlement_date <= date_to,
                ),
                and_(
                    OpenItemSettlement.created_at
                    >= datetime.combine(date_from, time.min, tzinfo=timezone.utc),
                    OpenItemSettlement.created_at
                    < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc),
                ),
            ),
        )
        .order_by(VatReturn.date_from, OpenItem.reference)
    ).all()
    if late_payments:
        warnings.append(
            "Istversteuerung: Diese Zahlungen liegen in einem schon gespeicherten Zeitraum, wurden "
            "aber erst danach zugeordnet. Dort fehlt ihre Umsatzsteuer, bitte berichtigen: "
            + ", ".join(f"{reference} ({label})" for reference, label in late_payments[:10])
            + (" …" if len(late_payments) > 10 else "")
        )
    without_item = sorted(
        entry.posting_number
        for key, entry in result.items()
        if isinstance(key, int)
        and entry.reversal_of_id is None
        and date_from <= entry.entry_date <= date_to
        and company_on_cash_basis(company, entry.entry_date)
        and any(_output_tax_of_row(row, vat_accounts) != ZERO for row in entry.rows)
        and any(
            row.account_subledger == SUBLEDGER_DEBTOR and row.debit_amount > ZERO
            for row in entry.rows
        )
    )
    if without_item:
        warnings.append(
            "Istversteuerung: Für diese Rechnungen auf Forderungskonten gibt es keinen "
            "verknüpften offenen Posten, sie zählen deshalb mit dem Buchungsdatum: "
            + ", ".join(without_item[:10])
            + (" …" if len(without_item) > 10 else "")
        )
    return result, warnings, foreign_events


def _floor_euro(value: Decimal) -> Decimal:
    """Bemessungsgrundlagen werden in vollen Euro (abgerundet) gemeldet."""
    return value.quantize(Decimal("1"), rounding=ROUND_DOWN)


def compute_vat_return_details(
    *,
    session: Session,
    company_id: int,
    date_from: date,
    date_to: date,
    include_closing_entries: bool = False,
) -> VatReturnResult:
    """Berechnet die UStVA-Kennziffern für den Zeitraum aus den Journaldaten.

    Zeilen mit Steuercode werden direkt zugeordnet. Zeilen ohne Steuercode
    (z. B. importierte oder manuelle Buchungen) werden datengetrieben
    ausgewertet: Steuerzeilen über die Steuerkonten (Steuercodes und
    Standard-Steuerkonten), die Bemessungsgrundlage über Ertragszeilen derselben
    Buchung; der Steuersatz wird aus dem Verhältnis USt/Bemessungsgrundlage
    abgeleitet. Erträge ohne Umsatzsteuer ordnet die DATEV-Kontenfunktion des
    Kontos einer Kennzahl zu (z. B. 8125/4125 → Kz 41, 8336/4336 → Kz 21); ohne
    Funktion bleiben sie außerhalb der UStVA und erscheinen als Hinweis.
    Innergemeinschaftliche Erwerbe und § 13b-Leistungen (Leistungsempfänger)
    erkennt die UStVA an ihren Steuerkonten (1574/1774, 1577/1787 bzw.
    1404/3804, 1407/3837); die Bemessungsgrundlage liefern die Aufwands- bzw.
    Anlagenzeilen derselben Buchung.
    """
    chart = detect_company_chart(session=session, company_id=company_id)
    vat_accounts = _company_vat_accounts(session, company_id)
    special_accounts = company_special_tax_accounts(session=session, company_id=company_id)
    partners = {
        partner.id: partner_vat_country(partner.country_code, partner.vat_id)
        for partner in session.execute(
            select(BusinessPartner).where(BusinessPartner.company_id == company_id)
        ).scalars()
    }
    known_rates = {
        Decimal(str(rate)).quantize(Decimal("0.01"))
        for (rate,) in session.execute(
            select(TaxCode.rate).where(TaxCode.company_id == company_id)
        ).all()
        if rate is not None and rate > 0
    } | {Decimal("19.00"), Decimal("7.00")}

    base_by_rate: dict[Decimal, Decimal] = {}
    bases: dict[str, Decimal] = {}
    output_tax = ZERO
    input_tax = ZERO
    special_input = {"ig_erwerb": ZERO, "reverse_charge": ZERO}
    unassigned: list[dict[str, object]] = []
    unclassified_reverse_charge: list[str] = []
    unknown_acquisition_rates: list[str] = []

    def add(kennzahl: str, amount: Decimal) -> None:
        bases[kennzahl] = bases.get(kennzahl, ZERO) + amount

    entries = vat_entries(session, company_id, date_from, date_to, include_closing_entries)
    cash_basis_warnings: list[str] = []
    foreign_payments: list[ForeignPaymentEvent] = []
    company = session.get(Company, company_id)
    if company is not None and company.vat_cash_basis_from is not None:
        entries, cash_basis_warnings, foreign_payments = _apply_cash_basis(
            session=session,
            company=company,
            entries=entries,
            date_from=date_from,
            date_to=date_to,
            vat_accounts=vat_accounts,
        )

    for entry in entries.values():

        def due(rule: str, entry: _Entry = entry) -> bool:
            # Gehört dieser Teil der Buchung (Umsatz, Vorsteuer, Erwerb …) in den Zeitraum?
            return date_from <= entry.tax_point(rule) <= date_to

        untagged_output = ZERO
        untagged_base = ZERO
        base_lines = []
        special_output: dict[tuple[str, Decimal | None], Decimal] = {}
        special_output_debit = False
        entry_special_input = {"ig_erwerb": ZERO, "reverse_charge": ZERO}
        purchase_lines = []
        has_regular_tax = False
        partner_countries = {
            partners.get(row.partner_id) for row in entry.rows if row.partner_id is not None
        } - {None}

        for row in entry.rows:
            special = special_accounts.get(row.account_id)
            if special is not None:
                if special.kind == TAX_KIND_INPUT:
                    entry_special_input[special.category] += row.debit_amount - row.credit_amount
                else:
                    key = (special.category, special.rate)
                    special_output[key] = (
                        special_output.get(key, ZERO) + row.credit_amount - row.debit_amount
                    )
                    special_output_debit = row.debit_amount > ZERO
                continue

            is_regular_tax = (
                row.vat_account_id is not None and row.account_id == row.vat_account_id
                if row.tax_code_id is not None
                else row.account_id in vat_accounts
            )
            if is_regular_tax:
                has_regular_tax = True
            elif row.line_account_type in {"expense", "asset"}:
                purchase_lines.append(row)

            if row.tax_code_id is not None:
                is_tax_line = (
                    row.vat_account_id is not None and row.account_id == row.vat_account_id
                )
                if is_tax_line:
                    if row.tax_kind == TAX_KIND_INPUT:
                        # Vorsteuer: Sollsaldo (Storno bucht Haben und mindert).
                        if due(INPUT):
                            input_tax += row.debit_amount - row.credit_amount
                    elif due(SERVICE):
                        # Umsatzsteuer: Habensaldo.
                        output_tax += row.credit_amount - row.debit_amount
                    continue

                if row.rate == ZERO:
                    # Steuerfreie Umsätze: nur Basiszeilen auf Ertragskonten; die
                    # Kontenfunktion verfeinert die Kennzahl, sonst Kz 48.
                    if row.line_account_type in {"revenue", "income"}:
                        kennzahl = revenue_kennzahl(chart, row.account_code) or "48"
                        if due(revenue_tax_rule(kennzahl)):
                            add(kennzahl, row.credit_amount - row.debit_amount)
                    continue

                if row.tax_kind == TAX_KIND_INPUT:
                    # Bemessungsgrundlagen von Eingangsleistungen werden in der UStVA
                    # nicht gemeldet (nur die Vorsteuer, Kz 66).
                    continue

                if due(SERVICE):
                    base_by_rate[row.rate] = (
                        base_by_rate.get(row.rate, ZERO) + row.credit_amount - row.debit_amount
                    )
                continue

            # Ohne Steuercode: Steuerkonten über ``company_tax_accounts`` erkennen.
            vat_kind = vat_accounts.get(row.account_id)
            if vat_kind == TAX_KIND_INPUT:
                if due(INPUT):
                    input_tax += row.debit_amount - row.credit_amount
                continue
            if vat_kind is not None:
                amount = row.credit_amount - row.debit_amount
                if due(SERVICE):
                    output_tax += amount
                untagged_output += amount
                continue
            if row.line_account_type in {"revenue", "income"}:
                amount = row.credit_amount - row.debit_amount
                kennzahl = revenue_kennzahl(chart, row.account_code)
                if kennzahl is not None:
                    if due(revenue_tax_rule(kennzahl)):
                        add(kennzahl, amount)
                else:
                    untagged_base += amount
                    base_lines.append((row, amount))

        # Erträge ohne Steuercode: mit USt-Zeile zum abgeleiteten Steuersatz,
        # ohne USt-Zeile und ohne Kontenfunktion nicht in der UStVA (Hinweis).
        if untagged_base != ZERO and due(SERVICE):
            if untagged_output != ZERO:
                rate = _match_rate(untagged_output / untagged_base * Decimal("100"), known_rates)
                base_by_rate[rate] = base_by_rate.get(rate, ZERO) + untagged_base
            else:
                unassigned.extend(
                    {
                        "posting_number": entry.posting_number,
                        "entry_date": entry.entry_date.isoformat(),
                        "account_code": row.account_code,
                        "account_name": row.account_name,
                        "amount": str(amount.quantize(ZERO)),
                    }
                    for row, amount in base_lines
                    if amount != ZERO
                )

        # Ig. Erwerb und § 13b als Leistungsempfänger: Bemessungsgrundlage aus den
        # Aufwands-/Anlagenzeilen auf der Gegenseite der Umsatzsteuer.
        purchase_base = sum(
            (
                row.debit_amount - row.credit_amount
                for row in purchase_lines
                if (row.debit_amount > ZERO) != special_output_debit
            ),
            ZERO,
        )
        purchase_kennzahlen = {
            datev_account_function(chart, row.account_code).kennzahl for row in purchase_lines
        }
        # § 13b: Leistung eines EU-Unternehmers (Kz 46/47, Steuer im Zeitraum der
        # Leistung, § 13b Abs. 1) oder übrige Fälle (Kz 84/85, mit der Rechnung,
        # spätestens im Folgemonat, § 13b Abs. 2).
        if "46" in purchase_kennzahlen:
            reverse_charge_kennzahl = "46"
        elif "84" in purchase_kennzahlen:
            reverse_charge_kennzahl = "84"
        elif partner_countries & (EU_COUNTRY_CODES - {"DE"}):
            reverse_charge_kennzahl = "46"
        elif partner_countries:
            reverse_charge_kennzahl = "84"
        else:
            reverse_charge_kennzahl = None
        reverse_charge_rule = SERVICE if reverse_charge_kennzahl == "46" else INVOICE
        if due(INVOICE):
            special_input["ig_erwerb"] += entry_special_input["ig_erwerb"]
        if due(reverse_charge_rule):
            special_input["reverse_charge"] += entry_special_input["reverse_charge"]

        for (category, account_rate), tax in special_output.items():
            if tax == ZERO:
                continue
            if not due(INVOICE if category == "ig_erwerb" else reverse_charge_rule):
                continue
            output_tax += tax
            # Mit normaler Steuer in derselben Buchung (gemischte Rechnung) oder
            # mehreren Sondersteuern ist die Aufwandszeile nicht eindeutig: Die
            # Bemessungsgrundlage folgt dann aus Steuer ÷ Satz.
            base = purchase_base if len(special_output) == 1 and not has_regular_tax else ZERO
            rate = account_rate
            if rate is None and base != ZERO:
                rate = _match_rate(tax / base * Decimal("100"), known_rates)
            if rate is None:
                rate = Decimal("19")
            if base == ZERO:
                base = (tax * Decimal("100") / rate).quantize(ZERO)
            if category == "ig_erwerb":
                kennzahl = {Decimal("19"): "89", Decimal("7"): "93"}.get(rate.normalize())
                if kennzahl is None:
                    unknown_acquisition_rates.append(entry.posting_number)
                    continue
                add(kennzahl, base)
                continue
            kennzahl = reverse_charge_kennzahl or "84"
            if reverse_charge_kennzahl is None:
                unclassified_reverse_charge.append(entry.posting_number)
            add(kennzahl, base)
            add("47" if kennzahl == "46" else "85", tax)

    rows = [
        VatReturnRow(
            "81", KENNZAHL_LABELS["81"], _floor_euro(base_by_rate.get(Decimal("19.00"), ZERO))
        ),
        VatReturnRow(
            "86", KENNZAHL_LABELS["86"], _floor_euro(base_by_rate.get(Decimal("7.00"), ZERO))
        ),
    ]
    # Sonstige Steuersätze (z. B. Altbestände 16 %) als eigene Zeilen ausweisen.
    for rate in sorted(set(base_by_rate) - {Decimal("19.00"), Decimal("7.00")}):
        rows.append(
            VatReturnRow(
                "35",
                f"Umsätze zu anderen Steuersätzen ({format_rate(rate)} %, Bemessungsgrundlage)",
                _floor_euro(base_by_rate[rate]),
            )
        )
    for kennzahl in ("87", "41", "44", "42", "43"):
        if bases.get(kennzahl, ZERO) != ZERO:
            rows.append(
                VatReturnRow(kennzahl, KENNZAHL_LABELS[kennzahl], _floor_euro(bases[kennzahl]))
            )
    rows.append(VatReturnRow("48", KENNZAHL_LABELS["48"], _floor_euro(bases.get("48", ZERO))))
    for kennzahl in ("21", "45", "60", "89", "93", "46", "47", "84", "85"):
        if bases.get(kennzahl, ZERO) != ZERO:
            amount = bases[kennzahl]
            rows.append(
                VatReturnRow(
                    kennzahl,
                    KENNZAHL_LABELS[kennzahl],
                    amount.quantize(ZERO) if kennzahl in ("47", "85") else _floor_euro(amount),
                )
            )
    total_input = input_tax + special_input["ig_erwerb"] + special_input["reverse_charge"]
    rows.append(VatReturnRow("USt", KENNZAHL_LABELS["USt"], output_tax.quantize(ZERO)))
    rows.append(VatReturnRow("66", KENNZAHL_LABELS["66"], input_tax.quantize(ZERO)))
    if special_input["ig_erwerb"] != ZERO:
        rows.append(
            VatReturnRow("61", KENNZAHL_LABELS["61"], special_input["ig_erwerb"].quantize(ZERO))
        )
    if special_input["reverse_charge"] != ZERO:
        rows.append(
            VatReturnRow(
                "67", KENNZAHL_LABELS["67"], special_input["reverse_charge"].quantize(ZERO)
            )
        )
    rows.append(
        VatReturnRow("83", KENNZAHL_LABELS["83"], (output_tax - total_input).quantize(ZERO))
    )

    warnings = []
    if unassigned:
        total = sum((Decimal(item["amount"]) for item in unassigned), ZERO)
        warnings.append(
            f"Nicht in der UStVA: {len(unassigned)} Ertragszeile(n) ohne Umsatzsteuer auf "
            f"Konten ohne UStVA-Funktion ({format_amount(total)} €, z. B. Zinsen). "
            "Steuerfreie oder nicht steuerbare Umsätze auf das passende Konto buchen "
            "(z. B. 8125/4125 ig. Lieferung, 8336/4336 sonstige Leistung an EU-Unternehmer, "
            "8338/4338 Drittland, 8100/4100 § 4 Nr. 8 ff.) oder mit Steuercode „frei“ "
            "(Kz 48)."
        )
    if unclassified_reverse_charge:
        warnings.append(
            "§ 13b-Steuer ohne Angabe zum leistenden Unternehmer als Kz 84/85 gemeldet "
            f"(Buchungen {', '.join(unclassified_reverse_charge)}): Für sonstige Leistungen "
            "eines EU-Unternehmers (Kz 46/47) den Lieferanten mit USt-IdNr. als Partner "
            "zuordnen oder das Konto 3123/5923 verwenden."
        )
    if unknown_acquisition_rates:
        warnings.append(
            "Innergemeinschaftlicher Erwerb zu einem anderen Steuersatz als 19 % oder 7 % "
            f"(Buchungen {', '.join(unknown_acquisition_rates)}): nur in der Umsatzsteuer "
            "enthalten, keine Bemessungsgrundlage gemeldet."
        )
    if chart is None and (unassigned or bases):
        warnings.append(
            "Kontenrahmen nicht erkannt: Steuerfreie Umsätze lassen sich nicht über die "
            "DATEV-Kontenfunktion zuordnen."
        )
    return VatReturnResult(
        rows=rows,
        warnings=[*cash_basis_warnings, *warnings],
        unassigned=unassigned,
        foreign_payments=foreign_payments,
    )


def compute_vat_return(
    *,
    session: Session,
    company_id: int,
    date_from: date,
    date_to: date,
    include_closing_entries: bool = False,
) -> list[VatReturnRow]:
    """UStVA-Kennzahlen für den Zeitraum (``compute_vat_return_details`` ohne Hinweise)."""
    return compute_vat_return_details(
        session=session,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        include_closing_entries=include_closing_entries,
    ).rows


def _post_vat_fx_difference(
    *,
    session: Session,
    company: Company,
    period_label: str,
    date_to: date,
    events: list[ForeignPaymentEvent],
    changed_by: str,
) -> JournalEntry | None:
    """Gleicht das Steuerkonto an die gemeldete Steuer an, jede Zahlung und jedes Storno einmal.

    Die Rechnung hat die Steuer zum Rechnungskurs gebucht. Die Differenz gehört zur Kursdifferenz,
    die die Zahlung auf das Brutto festgehalten hat: Danach zeigt das Steuerkonto, was gemeldet ist,
    und die Kursdifferenz nur noch das Entgelt. Die Buchung zählt nicht in der UStVA.
    """
    pending = [event for event in events if not event.is_posted]
    by_account: dict[int, Decimal] = {}
    for event in pending:
        for account_id, difference in event.differences.items():
            by_account[account_id] = by_account.get(account_id, ZERO) + difference
    by_account = {
        account_id: difference for account_id, difference in by_account.items() if difference
    }
    if not by_account:
        return None

    description = f"Umsatzsteuer-Kursdifferenz {period_label} (§ 16 Abs. 6 UStG)"
    lines = [
        JournalLineInput(
            account_id=account_id,
            debit_amount=difference if difference > ZERO else ZERO,
            credit_amount=-difference if difference < ZERO else ZERO,
            description=description,
        )
        for account_id, difference in sorted(by_account.items())
    ]
    gain = sum((value for value in by_account.values() if value > ZERO), ZERO)
    loss = sum((-value for value in by_account.values() if value < ZERO), ZERO)
    try:
        if gain:
            lines.append(
                JournalLineInput(
                    account_id=fx_account(session, company.id, gain=True).id,
                    credit_amount=gain,
                    description=description,
                )
            )
        if loss:
            lines.append(
                JournalLineInput(
                    account_id=fx_account(session, company.id, gain=False).id,
                    debit_amount=loss,
                    description=description,
                )
            )
        entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                # Aus einem gesperrten Monat oder abgeschlossenen Jahr in den nächsten offenen.
                entry_date=first_open_posting_date(
                    session=session, company_id=company.id, on=date_to
                ),
                description=description,
                status="posted",
                changed_by=changed_by,
                lines=lines,
                source=SOURCE_VAT_FX_DIFFERENCE,
                expand_tax_lines=False,
            ),
            commit=False,
        )
    except (ExchangeRateError, JournalEntryCreationError, JournalEntryValidationError) as exc:
        session.rollback()
        raise VatReturnError(
            f"Die Steuerdifferenz zum Monatskurs ist nicht gebucht: {exc}"
        ) from exc
    for event in pending:
        if event.is_reversal:
            event.settlement.vat_fx_reversal_entry_id = entry.id
        else:
            event.settlement.vat_fx_entry_id = entry.id
    return entry


def save_vat_return(
    *,
    session: Session,
    company_id: int,
    period_label: str,
    changed_by: str,
    include_closing_entries: bool = False,
) -> VatReturn:
    """Hält Umsatzsteuer-Kennziffern als unveränderlichen Snapshot fest.

    Zahlungen auf Fremdwährungsrechnungen behalten dabei ihren Monatskurs, ein späterer Kurs ändert
    den Zeitraum nicht mehr. Ohne Monatskurs wird nicht festgehalten, und die Differenz zum
    Rechnungskurs wird auf dem Steuerkonto gebucht (ADR-010).
    """
    company = session.get(Company, company_id)
    if company is None:
        raise VatReturnError("Gesellschaft nicht gefunden.")

    date_from, date_to, period_label = period_bounds(period_label)

    existing = session.execute(
        select(VatReturn.id).where(
            VatReturn.company_id == company_id,
            VatReturn.period_label == period_label,
        )
    ).first()
    if existing:
        display_name = vat_return_display_name(period_label)
        raise VatReturnError(
            f"Für den Zeitraum {period_label} wurde bereits eine {display_name} festgehalten."
        )

    details = compute_vat_return_details(
        session=session,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        include_closing_entries=include_closing_entries,
    )
    rows = details.rows
    missing_rates = sorted(
        {
            (event.currency_code, event.rate_month)
            for event in details.foreign_payments
            if event.rate_missing
        }
    )
    if missing_rates:
        raise VatReturnError(
            "Es fehlt der Monatskurs für "
            + ", ".join(f"{code} {format_rate_month(month)}" for code, month in missing_rates)
            + ". Bitte unter Fremdwährung abrufen oder erfassen, dann festhalten."
        )
    for event in details.foreign_payments:
        if event.settlement.vat_rate is None:
            event.settlement.vat_rate = event.rate
    fx_entry = _post_vat_fx_difference(
        session=session,
        company=company,
        period_label=period_label,
        date_to=date_to,
        events=details.foreign_payments,
        changed_by=changed_by,
    )
    vat_return = VatReturn(
        tenant_id=company.tenant_id,
        company_id=company.id,
        period_label=period_label,
        date_from=date_from,
        date_to=date_to,
        kennzahlen=[
            {"kennziffer": row.kennziffer, "label": row.label, "amount": str(row.amount)}
            for row in rows
        ],
        status="erstellt",
        fx_journal_entry_id=fx_entry.id if fx_entry is not None else None,
        created_by=changed_by,
    )
    session.add(vat_return)
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="vat_return",
        entity_id=str(vat_return.id),
        action="created",
        changed_by=changed_by,
        payload={
            "period_label": period_label,
            "declaration_type": vat_return_kind_from_label(period_label),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "include_closing_entries": include_closing_entries,
            "kennzahlen": vat_return.kennzahlen,
            "foreign_payments": [
                serialize_foreign_payment(event) for event in details.foreign_payments
            ],
            "fx_journal_entry_id": vat_return.fx_journal_entry_id,
        },
    )
    session.commit()
    session.refresh(vat_return)
    return vat_return


VAT_METHOD_ACCRUAL = "soll"
VAT_METHOD_CASH = "ist"


def company_vat_method(company: Company, *, on: date | None = None) -> dict[str, object]:
    """Versteuerungsart der Gesellschaft: ``soll`` oder ``ist`` am Stichtag (Standard heute)."""
    day = on or date.today()
    return {
        "company_id": company.id,
        "method": VAT_METHOD_CASH if company_on_cash_basis(company, day) else VAT_METHOD_ACCRUAL,
        "as_of": day.isoformat(),
        "cash_basis_from": (
            company.vat_cash_basis_from.isoformat() if company.vat_cash_basis_from else None
        ),
        "cash_basis_until": (
            company.vat_cash_basis_until.isoformat() if company.vat_cash_basis_until else None
        ),
    }


def set_company_vat_method(
    *,
    session: Session,
    company: Company,
    method: str,
    valid_from: date,
    changed_by: str,
) -> list[str]:
    """Wechselt die Versteuerungsart zum Monatsersten und liefert Hinweise.

    Abgebildet wird ein zusammenhängender Zeitraum der Istversteuerung. Liegt der Wechsel zurück
    auf ``soll`` am Beginn dieses Zeitraums, war die Eingabe ein Irrtum und er wird entfernt.
    Festgehaltene Voranmeldungen ändern sich nicht; sie werden als Hinweis genannt.
    """
    if method not in {VAT_METHOD_ACCRUAL, VAT_METHOD_CASH}:
        raise VatReturnError("Versteuerungsart muss soll oder ist sein.")
    if valid_from.day != 1:
        raise VatReturnError("Die Versteuerungsart wechselt zum Ersten eines Monats.")

    before = company_vat_method(company)
    if method == VAT_METHOD_CASH:
        if company.vat_cash_basis_until is not None:
            raise VatReturnError(
                "Ein erneuter Wechsel zur Istversteuerung nach deren Ende wird nicht abgebildet."
            )
        if company.vat_cash_basis_from is not None and company.vat_cash_basis_from != valid_from:
            raise VatReturnError("Die Istversteuerung ist bereits eingerichtet.")
        company.vat_cash_basis_from = valid_from
    else:
        if company.vat_cash_basis_from is None:
            raise VatReturnError("Die Gesellschaft nutzt bereits die Sollversteuerung.")
        if valid_from < company.vat_cash_basis_from:
            raise VatReturnError("Das Datum liegt vor dem Beginn der Istversteuerung.")
        if valid_from == company.vat_cash_basis_from:
            company.vat_cash_basis_from = None
            company.vat_cash_basis_until = None
        else:
            company.vat_cash_basis_until = valid_from - timedelta(days=1)

    after = company_vat_method(company)
    if {key: before[key] for key in ("cash_basis_from", "cash_basis_until")} == {
        key: after[key] for key in ("cash_basis_from", "cash_basis_until")
    }:
        return []
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company",
        entity_id=str(company.id),
        action="vat_method_changed",
        changed_by=changed_by,
        payload={"before": before, "after": after, "valid_from": valid_from.isoformat()},
    )
    session.commit()
    saved = (
        session.execute(
            select(VatReturn.period_label).where(
                VatReturn.company_id == company.id, VatReturn.date_to >= valid_from
            )
        )
        .scalars()
        .all()
    )
    if saved:
        return [
            "Bereits festgehaltene Voranmeldungen bleiben unverändert: " + ", ".join(sorted(saved))
        ]
    return []


def list_vat_returns(*, session: Session, company_id: int) -> list[VatReturn]:
    return (
        session.execute(
            select(VatReturn)
            .where(VatReturn.company_id == company_id)
            .order_by(VatReturn.date_from.desc(), VatReturn.id.desc())
        )
        .scalars()
        .all()
    )
