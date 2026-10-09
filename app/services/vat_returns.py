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

Stornobuchungen neutralisieren sich automatisch, da mit Salden gerechnet wird.
Buchungen der Abschlussperiode (13) bleiben standardmäßig außen vor
(``include_closing_entries``); Ergebnis- und Saldovorträge des
Jahresabschlusses zählen nie als Umsatz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.account_chart_check import detect_company_chart
from app.services.audit_log import log_audit_event
from app.services.datev_account_functions import datev_account_function
from app.services.journal_entries import CARRYFORWARD_SOURCES
from app.services.tax_codes import company_special_tax_accounts, company_tax_accounts
from domain.models import (
    TAX_KIND_INPUT,
    Account,
    BusinessPartner,
    Company,
    JournalEntry,
    JournalEntryLine,
    Period,
    TaxCode,
    VatReturn,
)


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
class VatReturnResult:
    """UStVA-Kennzahlen mit Hinweisen und nicht zugeordneten Erträgen."""

    rows: list[VatReturnRow]
    warnings: list[str] = field(default_factory=list)
    # Erträge ohne Umsatzsteuer auf Konten ohne UStVA-Funktion (nicht gemeldet).
    unassigned: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _Entry:
    posting_number: str
    entry_date: date
    rows: list = field(default_factory=list)


def vat_entries(
    session: Session,
    company_id: int,
    date_from: date,
    date_to: date,
    include_closing_entries: bool,
) -> dict[int, _Entry]:
    """Buchungszeilen des Zeitraums je Buchung (ohne Saldovorträge)."""
    stmt = (
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntry.posting_number,
            JournalEntry.entry_date,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.account_id,
            JournalEntryLine.partner_id,
            JournalEntryLine.tax_code_id,
            TaxCode.rate,
            TaxCode.vat_account_id,
            TaxCode.kind.label("tax_kind"),
            Account.code.label("account_code"),
            Account.name.label("account_name"),
            Account.account_type.label("line_account_type"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Period, Period.id == JournalEntry.period_id)
        .outerjoin(TaxCode, TaxCode.id == JournalEntryLine.tax_code_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.entry_date >= date_from,
            JournalEntry.entry_date <= date_to,
            JournalEntry.source.notin_(CARRYFORWARD_SOURCES),
        )
        .order_by(JournalEntry.entry_date, JournalEntry.id, JournalEntryLine.line_number)
    )
    if not include_closing_entries:
        stmt = stmt.where(Period.is_closing.is_(False))
    entries: dict[int, _Entry] = {}
    for row in session.execute(stmt).all():
        entry = entries.setdefault(row.journal_entry_id, _Entry(row.posting_number, row.entry_date))
        entry.rows.append(row)
    return entries


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

    for entry in vat_entries(
        session, company_id, date_from, date_to, include_closing_entries
    ).values():
        untagged_output = ZERO
        untagged_base = ZERO
        base_lines = []
        special_output: dict[tuple[str, Decimal | None], Decimal] = {}
        special_output_debit = False
        purchase_lines = []
        partner_countries = {
            partners.get(row.partner_id) for row in entry.rows if row.partner_id is not None
        } - {None}

        for row in entry.rows:
            special = special_accounts.get(row.account_id)
            if special is not None:
                if special.kind == TAX_KIND_INPUT:
                    special_input[special.category] += row.debit_amount - row.credit_amount
                else:
                    key = (special.category, special.rate)
                    special_output[key] = (
                        special_output.get(key, ZERO) + row.credit_amount - row.debit_amount
                    )
                    special_output_debit = row.debit_amount > ZERO
                continue

            if row.line_account_type in {"expense", "asset"}:
                purchase_lines.append(row)

            if row.tax_code_id is not None:
                is_tax_line = (
                    row.vat_account_id is not None and row.account_id == row.vat_account_id
                )
                if is_tax_line:
                    if row.tax_kind == TAX_KIND_INPUT:
                        # Vorsteuer: Sollsaldo (Storno bucht Haben und mindert).
                        input_tax += row.debit_amount - row.credit_amount
                    else:
                        # Umsatzsteuer: Habensaldo.
                        output_tax += row.credit_amount - row.debit_amount
                    continue

                if row.rate == ZERO:
                    # Steuerfreie Umsätze: nur Basiszeilen auf Ertragskonten; die
                    # Kontenfunktion verfeinert die Kennzahl, sonst Kz 48.
                    if row.line_account_type in {"revenue", "income"}:
                        add(
                            revenue_kennzahl(chart, row.account_code) or "48",
                            row.credit_amount - row.debit_amount,
                        )
                    continue

                if row.tax_kind == TAX_KIND_INPUT:
                    # Bemessungsgrundlagen von Eingangsleistungen werden in der UStVA
                    # nicht gemeldet (nur die Vorsteuer, Kz 66).
                    continue

                base_by_rate[row.rate] = (
                    base_by_rate.get(row.rate, ZERO) + row.credit_amount - row.debit_amount
                )
                continue

            # Ohne Steuercode: Steuerkonten über ``company_tax_accounts`` erkennen.
            vat_kind = vat_accounts.get(row.account_id)
            if vat_kind == TAX_KIND_INPUT:
                input_tax += row.debit_amount - row.credit_amount
                continue
            if vat_kind is not None:
                amount = row.credit_amount - row.debit_amount
                output_tax += amount
                untagged_output += amount
                continue
            if row.line_account_type in {"revenue", "income"}:
                amount = row.credit_amount - row.debit_amount
                kennzahl = revenue_kennzahl(chart, row.account_code)
                if kennzahl is not None:
                    add(kennzahl, amount)
                else:
                    untagged_base += amount
                    base_lines.append((row, amount))

        # Erträge ohne Steuercode: mit USt-Zeile zum abgeleiteten Steuersatz,
        # ohne USt-Zeile und ohne Kontenfunktion nicht in der UStVA (Hinweis).
        if untagged_base != ZERO:
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
        for (category, account_rate), tax in special_output.items():
            if tax == ZERO:
                continue
            output_tax += tax
            base = purchase_base if len(special_output) == 1 else ZERO
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
            if "46" in purchase_kennzahlen:
                kennzahl = "46"
            elif "84" in purchase_kennzahlen:
                kennzahl = "84"
            elif partner_countries & (EU_COUNTRY_CODES - {"DE"}):
                kennzahl = "46"
            elif partner_countries:
                kennzahl = "84"
            else:
                kennzahl = "84"
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
                f"Umsätze zu anderen Steuersätzen ({rate} %, Bemessungsgrundlage)",
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
            f"Konten ohne UStVA-Funktion ({total.quantize(ZERO)} €, z. B. Zinsen). "
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
    return VatReturnResult(rows=rows, warnings=warnings, unassigned=unassigned)


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


def save_vat_return(
    *,
    session: Session,
    company_id: int,
    period_label: str,
    changed_by: str,
    include_closing_entries: bool = False,
) -> VatReturn:
    """Hält Umsatzsteuer-Kennziffern als unveränderlichen Snapshot fest."""
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

    rows = compute_vat_return(
        session=session,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        include_closing_entries=include_closing_entries,
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
        },
    )
    session.commit()
    session.refresh(vat_return)
    return vat_return


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
