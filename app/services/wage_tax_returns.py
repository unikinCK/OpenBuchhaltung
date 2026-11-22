"""Lohnsteuer-Anmeldung (§ 41a EStG) aus gebuchten Lohnläufen.

Anzumelden ist die im Anmeldungszeitraum einbehaltene Lohnsteuer; einbehalten wird bei der
Lohnzahlung (§ 38 Abs. 3 EStG). Ein Lohnlauf zählt deshalb zu dem Zeitraum, in den sein
Zahlungsdatum fällt. Die Einzelaufstellung nach Vorjahr, laufendem Jahr und Folgejahr
richtet sich nach dem Lohnzahlungszeitraum (Lohnmonat) des Laufs.

Kennzahlen (amtlicher Datensatz LStA, ab 2021):

* 86 Zahl der Arbeitnehmer
* 42 Summe der einzubehaltenden Lohnsteuer, 48 verbleibende Lohnsteuer (= 42, da pauschale
  Lohnsteuer, Seeschifffahrt und BAV-Förderbetrag nicht abgebildet sind)
* 49 Solidaritätszuschlag
* 61–64 Kirchensteuer nach Kirchensteuermerkmal
* 83 Gesamtbetrag
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.payroll_pap import CHURCH_DENOMINATIONS
from app.services.vat_returns import VatReturnError, period_bounds
from domain.models import (
    Company,
    JournalEntryLine,
    PayrollEmployee,
    PayrollRun,
    PayrollRunLine,
    WageTaxReturn,
)

ZERO = Decimal("0.00")

CHURCH_TAX_KENNZAHLEN = {
    "61": "Evangelische Kirchensteuer",
    "62": "Römisch-Katholische Kirchensteuer",
    "63": "Alt-Katholische Kirchensteuer",
    "64": "Israelitische / Jüdische Kultussteuer",
}
YEAR_PARTS = (("Vorjahr", "Vorjahr"), ("lfdJahr", "laufendes Jahr"), ("Folgejahr", "Folgejahr"))
SPLIT_KENNZAHLEN = ("42", "49", *CHURCH_TAX_KENNZAHLEN)


class WageTaxReturnError(ValueError):
    """Die Lohnsteuer-Anmeldung kann nicht berechnet oder festgehalten werden."""


@dataclass(slots=True)
class WageTaxReturnRow:
    kennzahl: str
    label: str
    amount: Decimal | int


@dataclass(slots=True)
class WageTaxReturnResult:
    company_id: int
    period_label: str
    date_from: date
    date_to: date
    due_date: date
    employee_count: int
    rows: list[WageTaxReturnRow]
    payroll_runs: list[dict[str, Any]]
    draft_runs: list[dict[str, Any]]
    liability: dict[str, Any]
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def amount(self, kennzahl: str) -> Decimal:
        for row in self.rows:
            if row.kennzahl == kennzahl:
                return Decimal(row.amount)
        return ZERO


def wage_tax_period_bounds(period_label: str) -> tuple[date, date, str]:
    """Monat ``JJJJ-MM``, Quartal ``JJJJ-Qn`` oder Kalenderjahr ``JJJJ``."""
    try:
        date_from, date_to, canonical = period_bounds(period_label)
    except VatReturnError as exc:
        raise WageTaxReturnError(
            f"Ungültiger Anmeldungszeitraum {period_label!r} "
            "(erwartet JJJJ-MM, JJJJ-Qn oder JJJJ)."
        ) from exc
    if "-H" in canonical:
        raise WageTaxReturnError("Ein Halbjahr ist kein Lohnsteuer-Anmeldungszeitraum.")
    return date_from, date_to, canonical


def elster_period_code(period_label: str) -> str:
    """Zeitraum-Schlüssel des amtlichen Datensatzes: 01–12, 41–44 oder 19 (Jahr)."""
    _from, _to, canonical = wage_tax_period_bounds(period_label)
    if "-" not in canonical:
        return "19"
    part = canonical.split("-", 1)[1]
    if part.startswith("Q"):
        return f"4{part[1:]}"
    return part


def wage_tax_due_date(date_to: date) -> date:
    """Abgabe und Zahlung bis zum 10. Tag nach Ablauf des Zeitraums (§ 41a Abs. 1 EStG).

    Fällt der Tag auf ein Wochenende, gilt der nächste Montag (§ 108 Abs. 3 AO); landesweite
    Feiertage sind nicht berücksichtigt.
    """
    due = date_to + timedelta(days=10)
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due


def compute_wage_tax_return(
    *, session: Session, company_id: int, period_label: str
) -> WageTaxReturnResult:
    company = session.get(Company, company_id)
    if company is None:
        raise WageTaxReturnError("Gesellschaft nicht gefunden.")
    date_from, date_to, period_label = wage_tax_period_bounds(period_label)
    year = date_to.year

    runs = (
        session.execute(
            select(PayrollRun)
            .where(
                PayrollRun.company_id == company_id,
                PayrollRun.payment_date >= date_from,
                PayrollRun.payment_date <= date_to,
            )
            .options(selectinload(PayrollRun.lines).selectinload(PayrollRunLine.employee))
            .order_by(PayrollRun.payment_date, PayrollRun.id)
        )
        .scalars()
        .all()
    )
    posted = [run for run in runs if run.status == "posted"]
    drafts = [run for run in runs if run.status != "posted"]

    split: dict[str, dict[str, Decimal]] = {
        kz: {part: ZERO for part, _label in YEAR_PARTS} for kz in SPLIT_KENNZAHLEN
    }
    employees: set[int] = set()
    issues: list[str] = []
    warnings: list[str] = []
    for run in posted:
        run_year = int(run.period_label[:4])
        part = "lfdJahr" if run_year == year else ("Vorjahr" if run_year < year else "Folgejahr")
        for line in run.lines:
            employees.add(line.employee_id)
            split["42"][part] += Decimal(line.wage_tax)
            split["49"][part] += Decimal(line.solidarity_surcharge)
            church_tax = Decimal(line.church_tax)
            if church_tax == ZERO:
                continue
            entry = CHURCH_DENOMINATIONS.get(line.church_denomination or "")
            if entry is None:
                issues.append(
                    f"Lohnlauf {run.period_label}: Kirchensteuer {church_tax} € für "
                    f"Mitarbeiter {line.employee.employee_number} ohne Kirchensteuermerkmal."
                )
                continue
            split[entry[1]][part] += church_tax
        if not _within_three_weeks(run):
            warnings.append(
                f"Lohnlauf {run.period_label} wurde erst am {run.payment_date.isoformat()} "
                "gezahlt und zählt deshalb zu diesem Anmeldungszeitraum."
            )
    for run in drafts:
        warnings.append(
            f"Lohnlauf {run.period_label} (Zahlung {run.payment_date.isoformat()}) ist noch nicht "
            "gebucht und fehlt in der Anmeldung."
        )
    if date_to >= date.today():
        warnings.append("Der Anmeldungszeitraum ist noch nicht abgelaufen.")

    rows: list[WageTaxReturnRow] = [
        WageTaxReturnRow("86", "Zahl der Arbeitnehmer", len(employees))
    ]
    totals = {kz: sum(parts.values(), ZERO) for kz, parts in split.items()}
    rows.extend(_split_rows("42", "Summe der einzubehaltenden Lohnsteuer", split, totals))
    rows.append(WageTaxReturnRow("48", "Verbleiben (Lohnsteuer)", totals["42"]))
    rows.extend(_split_rows("49", "Solidaritätszuschlag", split, totals))
    for kz, label in CHURCH_TAX_KENNZAHLEN.items():
        rows.extend(_split_rows(kz, label, split, totals))
    total = totals["42"] + totals["49"] + sum(
        (totals[kz] for kz in CHURCH_TAX_KENNZAHLEN), ZERO
    )
    rows.append(WageTaxReturnRow("83", "Gesamtbetrag", total))

    liability = _liability_check(session, posted, total)
    if liability["difference"] != "0.00":
        warnings.append(
            "Die Lohnsteuer-Verbindlichkeit aus den Lohnbuchungen weicht um "
            f"{liability['difference']} € vom Gesamtbetrag ab."
        )

    return WageTaxReturnResult(
        company_id=company_id,
        period_label=period_label,
        date_from=date_from,
        date_to=date_to,
        due_date=wage_tax_due_date(date_to),
        employee_count=len(employees),
        rows=rows,
        payroll_runs=[_run_summary(run) for run in posted],
        draft_runs=[_run_summary(run) for run in drafts],
        liability=liability,
        issues=issues,
        warnings=warnings,
    )


def save_wage_tax_return(
    *,
    session: Session,
    company_id: int,
    period_label: str,
    changed_by: str,
    correction: bool = False,
) -> WageTaxReturn:
    """Hält die Anmeldung als unveränderlichen Snapshot fest; Berichtigung mit ``correction``."""
    company = session.get(Company, company_id)
    if company is None:
        raise WageTaxReturnError("Gesellschaft nicht gefunden.")
    result = compute_wage_tax_return(
        session=session, company_id=company_id, period_label=period_label
    )
    if result.date_from > date.today():
        raise WageTaxReturnError(
            "Ein Anmeldungszeitraum in der Zukunft kann nicht festgehalten werden."
        )
    if result.issues:
        raise WageTaxReturnError(" ".join(result.issues))

    latest = session.execute(
        select(func.max(WageTaxReturn.correction_number)).where(
            WageTaxReturn.company_id == company_id,
            WageTaxReturn.period_label == result.period_label,
        )
    ).scalar_one_or_none()
    if latest is not None and not correction:
        raise WageTaxReturnError(
            f"Für {result.period_label} ist bereits eine Lohnsteuer-Anmeldung festgehalten; "
            "für eine Änderung bitte als Berichtigung festhalten."
        )
    if latest is None and correction:
        raise WageTaxReturnError(
            f"Für {result.period_label} gibt es noch keine Anmeldung, "
            "die berichtigt werden könnte."
        )
    correction_number = 0 if latest is None else latest + 1

    wage_tax_return = WageTaxReturn(
        tenant_id=company.tenant_id,
        company_id=company.id,
        period_label=result.period_label,
        date_from=result.date_from,
        date_to=result.date_to,
        correction_number=correction_number,
        employee_count=result.employee_count,
        kennzahlen=serialize_rows(result.rows),
        payroll_run_ids=[run["id"] for run in result.payroll_runs],
        status="erstellt",
        created_by=changed_by,
    )
    session.add(wage_tax_return)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="wage_tax_return",
        entity_id=str(wage_tax_return.id),
        action="created",
        changed_by=changed_by,
        payload={
            "period_label": wage_tax_return.period_label,
            "correction_number": correction_number,
            "date_from": result.date_from.isoformat(),
            "date_to": result.date_to.isoformat(),
            "kennzahlen": wage_tax_return.kennzahlen,
            "payroll_run_ids": wage_tax_return.payroll_run_ids,
            "warnings": result.warnings,
        },
    )
    session.commit()
    session.refresh(wage_tax_return)
    return wage_tax_return


def list_wage_tax_returns(*, session: Session, company_id: int) -> list[WageTaxReturn]:
    return (
        session.execute(
            select(WageTaxReturn)
            .where(WageTaxReturn.company_id == company_id)
            .order_by(
                WageTaxReturn.date_from.desc(),
                WageTaxReturn.correction_number.desc(),
                WageTaxReturn.id.desc(),
            )
        )
        .scalars()
        .all()
    )


def serialize_rows(rows: list[WageTaxReturnRow]) -> list[dict[str, Any]]:
    return [
        {"kennzahl": row.kennzahl, "label": row.label, "amount": str(row.amount)}
        for row in rows
    ]


def _split_rows(
    kennzahl: str,
    label: str,
    split: dict[str, dict[str, Decimal]],
    totals: dict[str, Decimal],
) -> list[WageTaxReturnRow]:
    """Kennzahl und – sobald sie nicht 0 ist – ihre Einzelaufstellung nach Jahren."""
    if kennzahl not in {"42", "49"} and totals[kennzahl] == ZERO:
        return []
    rows = [WageTaxReturnRow(kennzahl, label, totals[kennzahl])]
    if totals[kennzahl] != ZERO:
        for part, part_label in YEAR_PARTS:
            amount = split[kennzahl][part]
            if amount != ZERO:
                rows.append(
                    WageTaxReturnRow(f"{kennzahl}_{part}", f"{label}: {part_label}", amount)
                )
    return rows


def _within_three_weeks(run: PayrollRun) -> bool:
    """Zahlung bis drei Wochen nach Ende des Lohnmonats (üblicher Zahlungsrhythmus)."""
    year, month = (int(part) for part in run.period_label.split("-", 1))
    first = date(year, month, 1)
    month_end = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first <= run.payment_date <= month_end + timedelta(days=21)


def _run_summary(run: PayrollRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "period_label": run.period_label,
        "payment_date": run.payment_date.isoformat(),
        "status": run.status,
        "journal_entry_id": run.journal_entry_id,
        "employee_count": len(run.lines),
        "wage_tax_total": str(run.wage_tax_total),
        "church_tax_total": str(run.church_tax_total),
        "solidarity_surcharge_total": str(run.solidarity_surcharge_total),
    }


def _liability_check(
    session: Session, runs: list[PayrollRun], total: Decimal
) -> dict[str, Any]:
    """Haben-Saldo der Lohnbuchungen auf den Lohnsteuer-Verbindlichkeitskonten."""
    account_ids = sorted(
        {
            line.employee.wage_tax_liability_account_id
            for run in runs
            for line in run.lines
            if isinstance(line.employee, PayrollEmployee)
        }
    )
    entry_ids = [run.journal_entry_id for run in runs if run.journal_entry_id]
    booked = ZERO
    if account_ids and entry_ids:
        booked = session.execute(
            select(
                func.coalesce(
                    func.sum(JournalEntryLine.credit_amount - JournalEntryLine.debit_amount), 0
                )
            ).where(
                JournalEntryLine.journal_entry_id.in_(entry_ids),
                JournalEntryLine.account_id.in_(account_ids),
            )
        ).scalar_one()
    booked = Decimal(booked).quantize(Decimal("0.01"))
    return {
        "account_ids": account_ids,
        "journal_entry_ids": entry_ids,
        "booked": str(booked),
        "declared": str(total.quantize(Decimal("0.01"))),
        "difference": str((booked - total).quantize(Decimal("0.01"))),
    }
