"""Umsatzsteuer-Voranmeldung (UStVA): Kennziffern-Berechnung und Snapshots.

Die Berechnung leitet die UStVA-Kennziffern aus den Journaldaten ab. Grundlage
sind Buchungszeilen mit Steuercode:

* **Steuerzeilen** (Zeile liegt auf dem Steuerkonto des Steuercodes) liefern die
  gebuchte Umsatz- bzw. Vorsteuer.
* **Basiszeilen** (übrige Zeilen mit Steuercode) liefern die
  Bemessungsgrundlagen.

Buchungszeilen **ohne Steuercode** (importierte oder manuelle Buchungen) werden
datengetrieben ausgewertet: Zeilen auf Konten, die ein Steuercode der
Gesellschaft als Steuerkonto referenziert, zählen als Umsatz-/Vorsteuerzeilen;
Ertragszeilen derselben Buchung bilden die Bemessungsgrundlage, deren
Steuersatz aus dem Verhältnis USt/Bemessungsgrundlage abgeleitet wird.
Ertragsbuchungen ohne Umsatzsteuerzeile gelten als steuerfrei (Kz 48).

Die Richtung ergibt sich datengetrieben aus dem Kontotyp des Steuerkontos:
``liability`` = Umsatzsteuer (Ausgangsumsätze), ``asset`` = Vorsteuer
(Eingangsleistungen). Steuerfreie Umsätze (Steuersatz 0 %) werden über
Basiszeilen auf Erlöskonten erkannt.

Kennziffern (amtliches UStVA-Formular, Basisfälle):
* Kz 81: Steuerpflichtige Umsätze 19 % (Bemessungsgrundlage, volle EUR)
* Kz 86: Steuerpflichtige Umsätze 7 % (Bemessungsgrundlage, volle EUR)
* Kz 48: Steuerfreie Umsätze ohne Vorsteuerabzug bzw. 0 %-Fälle (vereinfachte
  Zuordnung für Basisfälle)
* Kz 66: Abziehbare Vorsteuerbeträge
* Kz 83: Verbleibende USt-Vorauszahlung bzw. Überschuss (gebuchte USt − VSt)

Stornobuchungen neutralisieren sich automatisch, da mit Salden gerechnet wird.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import (
    Account,
    Company,
    JournalEntry,
    JournalEntryLine,
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
    """Steuerkonten der Gesellschaft laut Steuercode-Definitionen.

    Liefert ``{account_id: account_type}`` für alle Konten, die von einem
    Steuercode als Steuerkonto referenziert werden. Über diese Zuordnung werden
    auch Buchungszeilen ohne Steuercode als Umsatz-/Vorsteuerzeilen erkannt.
    """
    rows = session.execute(
        select(TaxCode.vat_account_id, Account.account_type)
        .join(Account, Account.id == TaxCode.vat_account_id)
        .where(TaxCode.company_id == company_id, TaxCode.vat_account_id.is_not(None))
    ).all()
    return {row.vat_account_id: row.account_type for row in rows}


def _match_rate(raw_rate: Decimal, known_rates: set[Decimal]) -> Decimal:
    """Ordnet einen rechnerisch abgeleiteten Steuersatz dem nächsten bekannten zu."""
    best = min(known_rates, key=lambda rate: abs(rate - raw_rate), default=None)
    if best is not None and abs(best - raw_rate) <= Decimal("1.0"):
        return best
    return raw_rate.quantize(Decimal("0.01"))


def compute_vat_return(
    *,
    session: Session,
    company_id: int,
    date_from: date,
    date_to: date,
) -> list[VatReturnRow]:
    """Berechnet die UStVA-Kennziffern für den Zeitraum aus den Journaldaten.

    Zeilen mit Steuercode werden direkt zugeordnet. Zeilen ohne Steuercode
    (z. B. importierte oder manuelle Buchungen) werden datengetrieben
    ausgewertet: Steuerzeilen über die Steuerkonten der Steuercodes, die
    Bemessungsgrundlage über Ertragszeilen derselben Buchung; der Steuersatz
    wird aus dem Verhältnis USt/Bemessungsgrundlage abgeleitet. Ertragsbuchungen
    ganz ohne Umsatzsteuerzeile gelten als steuerfrei (Kz 48).
    """
    line_account = Account.__table__.alias("line_account")
    vat_account = Account.__table__.alias("vat_account")

    rows = session.execute(
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.account_id,
            JournalEntryLine.tax_code_id,
            TaxCode.rate,
            TaxCode.vat_account_id,
            line_account.c.account_type.label("line_account_type"),
            vat_account.c.account_type.label("vat_account_type"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .outerjoin(TaxCode, TaxCode.id == JournalEntryLine.tax_code_id)
        .join(line_account, line_account.c.id == JournalEntryLine.account_id)
        .outerjoin(vat_account, vat_account.c.id == TaxCode.vat_account_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.entry_date >= date_from,
            JournalEntry.entry_date <= date_to,
        )
    ).all()

    vat_accounts = _company_vat_accounts(session, company_id)
    known_rates = {
        Decimal(str(rate)).quantize(Decimal("0.01"))
        for (rate,) in session.execute(
            select(TaxCode.rate).where(TaxCode.company_id == company_id)
        ).all()
        if rate is not None and rate > 0
    } | {Decimal("19.00"), Decimal("7.00")}

    base_by_rate: dict[Decimal, Decimal] = {}
    tax_free_base = ZERO
    output_tax = ZERO
    input_tax = ZERO

    # Zeilen ohne Steuercode werden je Buchung gesammelt, um den Steuersatz aus
    # dem Verhältnis von USt-Zeile zu Ertragszeilen ableiten zu können.
    untagged_output_by_entry: dict[int, Decimal] = {}
    untagged_base_by_entry: dict[int, Decimal] = {}

    for row in rows:
        if row.tax_code_id is not None:
            is_tax_line = (
                row.vat_account_id is not None and row.account_id == row.vat_account_id
            )
            if is_tax_line:
                if row.vat_account_type == "asset":
                    # Vorsteuer: Sollsaldo (Storno bucht Haben und mindert).
                    input_tax += row.debit_amount - row.credit_amount
                else:
                    # Umsatzsteuer: Habensaldo.
                    output_tax += row.credit_amount - row.debit_amount
                continue

            if row.rate == ZERO:
                # Steuerfreie Umsätze: nur Basiszeilen auf Ertragskonten.
                if row.line_account_type in {"revenue", "income"}:
                    tax_free_base += row.credit_amount - row.debit_amount
                continue

            if row.vat_account_type == "asset":
                # Bemessungsgrundlagen von Eingangsleistungen werden in der UStVA
                # nicht gemeldet (nur die Vorsteuer, Kz 66).
                continue

            base_by_rate[row.rate] = (
                base_by_rate.get(row.rate, ZERO) + row.credit_amount - row.debit_amount
            )
            continue

        # Ohne Steuercode: Steuerkonten über die Steuercode-Definitionen erkennen.
        vat_account_type = vat_accounts.get(row.account_id)
        if vat_account_type == "asset":
            input_tax += row.debit_amount - row.credit_amount
            continue
        if vat_account_type is not None:
            amount = row.credit_amount - row.debit_amount
            output_tax += amount
            untagged_output_by_entry[row.journal_entry_id] = (
                untagged_output_by_entry.get(row.journal_entry_id, ZERO) + amount
            )
            continue
        if row.line_account_type in {"revenue", "income"}:
            untagged_base_by_entry[row.journal_entry_id] = (
                untagged_base_by_entry.get(row.journal_entry_id, ZERO)
                + row.credit_amount
                - row.debit_amount
            )

    # Bemessungsgrundlagen ohne Steuercode je Buchung zuordnen: mit USt-Zeile
    # zum abgeleiteten Steuersatz, ohne USt-Zeile als steuerfrei (Kz 48).
    for entry_id, base in untagged_base_by_entry.items():
        if base == ZERO:
            continue
        tax = untagged_output_by_entry.get(entry_id, ZERO)
        if tax != ZERO:
            rate = _match_rate(tax / base * Decimal("100"), known_rates)
            base_by_rate[rate] = base_by_rate.get(rate, ZERO) + base
        else:
            tax_free_base += base

    def _floor_euro(value: Decimal) -> Decimal:
        """Bemessungsgrundlagen werden in vollen Euro (abgerundet) gemeldet."""
        return value.quantize(Decimal("1"), rounding=ROUND_DOWN)

    result = [
        VatReturnRow(
            kennziffer="81",
            label="Steuerpflichtige Umsätze 19 % (Bemessungsgrundlage)",
            amount=_floor_euro(base_by_rate.get(Decimal("19.00"), ZERO)),
        ),
        VatReturnRow(
            kennziffer="86",
            label="Steuerpflichtige Umsätze 7 % (Bemessungsgrundlage)",
            amount=_floor_euro(base_by_rate.get(Decimal("7.00"), ZERO)),
        ),
        VatReturnRow(
            kennziffer="48",
            label="Steuerfreie Umsätze (Bemessungsgrundlage)",
            amount=_floor_euro(tax_free_base),
        ),
        VatReturnRow(
            kennziffer="USt",
            label="Gebuchte Umsatzsteuer",
            amount=output_tax.quantize(ZERO),
        ),
        VatReturnRow(
            kennziffer="66",
            label="Abziehbare Vorsteuerbeträge",
            amount=input_tax.quantize(ZERO),
        ),
        VatReturnRow(
            kennziffer="83",
            label="Verbleibende Vorauszahlung / Überschuss",
            amount=(output_tax - input_tax).quantize(ZERO),
        ),
    ]

    # Sonstige Steuersätze (z. B. Altbestände 16 %) als eigene Zeilen ausweisen.
    for rate in sorted(set(base_by_rate) - {Decimal("19.00"), Decimal("7.00")}):
        result.insert(
            2,
            VatReturnRow(
                kennziffer="35",
                label=f"Umsätze zu anderen Steuersätzen ({rate} %, Bemessungsgrundlage)",
                amount=_floor_euro(base_by_rate[rate]),
            ),
        )

    return result


def save_vat_return(
    *,
    session: Session,
    company_id: int,
    period_label: str,
    changed_by: str,
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
        session=session, company_id=company_id, date_from=date_from, date_to=date_to
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
