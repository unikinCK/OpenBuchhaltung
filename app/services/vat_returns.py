"""Umsatzsteuer-Voranmeldung (UStVA): Kennziffern-Berechnung und Snapshots.

Die Berechnung leitet die UStVA-Kennziffern aus den Journaldaten ab. Grundlage
sind Buchungszeilen mit Steuercode:

* **Steuerzeilen** (Zeile liegt auf dem Steuerkonto des Steuercodes) liefern die
  gebuchte Umsatz- bzw. Vorsteuer.
* **Basiszeilen** (übrige Zeilen mit Steuercode) liefern die
  Bemessungsgrundlagen.

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


def period_bounds(period_label: str) -> tuple[date, date, str]:
    """Zeitraumgrenzen und kanonisches Label für "JJJJ-MM" oder "JJJJ-Qn"."""
    try:
        year_raw, part = period_label.strip().upper().split("-", 1)
        year = int(year_raw)
        if part.startswith("Q"):
            quarter = int(part[1:])
            if quarter not in {1, 2, 3, 4}:
                raise ValueError
            start = date(year, 3 * quarter - 2, 1)
            end_month = 3 * quarter
            canonical = f"{year}-Q{quarter}"
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
            f"Ungültiger Voranmeldungszeitraum {period_label!r} "
            "(erwartet JJJJ-MM oder JJJJ-Qn)."
        ) from None


def compute_vat_return(
    *,
    session: Session,
    company_id: int,
    date_from: date,
    date_to: date,
) -> list[VatReturnRow]:
    """Berechnet die UStVA-Kennziffern für den Zeitraum aus den Journaldaten."""
    line_account = Account.__table__.alias("line_account")
    vat_account = Account.__table__.alias("vat_account")

    rows = session.execute(
        select(
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.account_id,
            TaxCode.rate,
            TaxCode.vat_account_id,
            line_account.c.account_type.label("line_account_type"),
            vat_account.c.account_type.label("vat_account_type"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(TaxCode, TaxCode.id == JournalEntryLine.tax_code_id)
        .join(line_account, line_account.c.id == JournalEntryLine.account_id)
        .outerjoin(vat_account, vat_account.c.id == TaxCode.vat_account_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.entry_date >= date_from,
            JournalEntry.entry_date <= date_to,
        )
    ).all()

    base_by_rate: dict[Decimal, Decimal] = {}
    tax_free_base = ZERO
    output_tax = ZERO
    input_tax = ZERO

    for row in rows:
        is_tax_line = row.vat_account_id is not None and row.account_id == row.vat_account_id
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
    """Hält die UStVA für den Zeitraum als unveränderlichen Snapshot fest."""
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
        raise VatReturnError(
            f"Für den Zeitraum {period_label} wurde bereits eine UStVA festgehalten."
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
