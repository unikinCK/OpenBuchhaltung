"""Lohnbuchhaltung MVP: Mitarbeiter, Lohnlauf und FiBu-Buchung.

Die Berechnung nutzt bewusst konfigurierbare Raten je Mitarbeiter. Amtliche
Lohnsteuer-/Sozialversicherungsberechnungen (PAP, ELStAM, DEUEV) sind nicht Teil
dieses MVPs und muessen spaeter als eigene Fachmodule angebunden werden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.payroll_pap import PayrollPapError, calculate_pap_wage_tax
from domain.models import Account, Company, PayrollEmployee, PayrollRun, PayrollRunLine


class PayrollError(ValueError):
    """Raised when payroll data cannot be created or posted."""


@dataclass(slots=True)
class PayrollEmployeeInput:
    company_id: int
    employee_number: str
    first_name: str
    last_name: str
    employment_start: date
    gross_monthly_salary: Decimal
    wage_expense_account_id: int | None = None
    wage_expense_account_code: str | None = None
    employer_social_security_expense_account_id: int | None = None
    employer_social_security_expense_account_code: str | None = None
    payroll_liability_account_id: int | None = None
    payroll_liability_account_code: str | None = None
    wage_tax_liability_account_id: int | None = None
    wage_tax_liability_account_code: str | None = None
    social_security_liability_account_id: int | None = None
    social_security_liability_account_code: str | None = None
    employment_end: date | None = None
    status: str = "active"
    tax_id: str | None = None
    birth_date: date | None = None
    tax_class: int = 1
    child_allowances: Decimal = Decimal("0.0")
    federal_state: str | None = None
    main_employment: bool = True
    social_security_number: str | None = None
    wage_tax_rate: Decimal = Decimal("0.00")
    church_tax_rate: Decimal = Decimal("0.00")
    solidarity_surcharge_rate: Decimal = Decimal("0.00")
    employee_social_security_rate: Decimal = Decimal("0.00")
    employer_social_security_rate: Decimal = Decimal("0.00")
    changed_by: str = "system"


@dataclass(slots=True)
class PayrollRunInput:
    company_id: int
    period_label: str
    payment_date: date
    employee_ids: list[int] | None = None
    changed_by: str = "system"
    config: Mapping[str, object] | None = None


MONEY = Decimal("0.01")


def create_payroll_employee(
    *, session: Session, payload: PayrollEmployeeInput
) -> PayrollEmployee:
    company = session.get(Company, payload.company_id)
    if company is None:
        raise PayrollError("Gesellschaft nicht gefunden.")
    if not payload.employee_number.strip():
        raise PayrollError("Personalnummer ist Pflicht.")
    if not payload.first_name.strip() or not payload.last_name.strip():
        raise PayrollError("Vor- und Nachname sind Pflicht.")
    gross_salary = _money(payload.gross_monthly_salary)
    if gross_salary <= Decimal("0.00"):
        raise PayrollError("Bruttogehalt muss positiv sein.")
    if payload.employment_end and payload.employment_end < payload.employment_start:
        raise PayrollError("Austrittsdatum darf nicht vor Eintritt liegen.")
    if payload.status not in {"active", "inactive"}:
        raise PayrollError("Status muss active oder inactive sein.")

    employee = PayrollEmployee(
        tenant_id=company.tenant_id,
        company_id=company.id,
        employee_number=payload.employee_number.strip(),
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        employment_start=payload.employment_start,
        employment_end=payload.employment_end,
        status=payload.status,
        birth_date=payload.birth_date,
        tax_id=(payload.tax_id or "").strip() or None,
        tax_class=_tax_class(payload.tax_class),
        child_allowances=Decimal(payload.child_allowances),
        federal_state=(payload.federal_state or "").strip().upper()[:2] or None,
        main_employment=payload.main_employment,
        social_security_number=(payload.social_security_number or "").strip() or None,
        gross_monthly_salary=gross_salary,
        wage_tax_rate=_rate(payload.wage_tax_rate),
        church_tax_rate=_rate(payload.church_tax_rate),
        solidarity_surcharge_rate=_rate(payload.solidarity_surcharge_rate),
        employee_social_security_rate=_rate(payload.employee_social_security_rate),
        employer_social_security_rate=_rate(payload.employer_social_security_rate),
        wage_expense_account_id=_account_id(
            session=session,
            company=company,
            account_id=payload.wage_expense_account_id,
            account_code=payload.wage_expense_account_code,
            label="Lohnaufwandskonto",
        ),
        employer_social_security_expense_account_id=_account_id(
            session=session,
            company=company,
            account_id=payload.employer_social_security_expense_account_id,
            account_code=payload.employer_social_security_expense_account_code,
            label="Arbeitgeber-SV-Aufwandskonto",
        ),
        payroll_liability_account_id=_account_id(
            session=session,
            company=company,
            account_id=payload.payroll_liability_account_id,
            account_code=payload.payroll_liability_account_code,
            label="Lohnverbindlichkeitskonto",
        ),
        wage_tax_liability_account_id=_account_id(
            session=session,
            company=company,
            account_id=payload.wage_tax_liability_account_id,
            account_code=payload.wage_tax_liability_account_code,
            label="Lohnsteuer-Verbindlichkeitskonto",
        ),
        social_security_liability_account_id=_account_id(
            session=session,
            company=company,
            account_id=payload.social_security_liability_account_id,
            account_code=payload.social_security_liability_account_code,
            label="SV-Verbindlichkeitskonto",
        ),
    )
    session.add(employee)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="payroll_employee",
        entity_id=str(employee.id),
        action="created",
        changed_by=payload.changed_by,
        payload={
            "employee_number": employee.employee_number,
            "status": employee.status,
            "gross_monthly_salary": str(employee.gross_monthly_salary),
        },
    )
    session.commit()
    session.refresh(employee)
    return employee


def list_payroll_employees(*, session: Session, company_id: int) -> list[PayrollEmployee]:
    return (
        session.execute(
            select(PayrollEmployee)
            .where(PayrollEmployee.company_id == company_id)
            .order_by(PayrollEmployee.last_name, PayrollEmployee.first_name)
        )
        .scalars()
        .all()
    )


def create_payroll_run(*, session: Session, payload: PayrollRunInput) -> PayrollRun:
    company = session.get(Company, payload.company_id)
    if company is None:
        raise PayrollError("Gesellschaft nicht gefunden.")
    period_label = _normalize_period(payload.period_label)
    employees = _payroll_employees_for_run(
        session=session,
        company_id=company.id,
        payment_date=payload.payment_date,
        employee_ids=payload.employee_ids,
    )
    if not employees:
        raise PayrollError("Keine aktiven Mitarbeiter für den Lohnlauf gefunden.")

    run = PayrollRun(
        tenant_id=company.tenant_id,
        company_id=company.id,
        period_label=period_label,
        payment_date=payload.payment_date,
        status="draft",
        created_by=payload.changed_by,
    )
    session.add(run)
    session.flush()

    totals = {
        "gross": Decimal("0.00"),
        "wage_tax": Decimal("0.00"),
        "church_tax": Decimal("0.00"),
        "solidarity": Decimal("0.00"),
        "employee_sv": Decimal("0.00"),
        "employer_sv": Decimal("0.00"),
        "net": Decimal("0.00"),
    }
    for employee in employees:
        line_values = calculate_payroll_line(
            employee,
            payment_date=payload.payment_date,
            period_label=period_label,
            config=payload.config,
        )
        session.add(
            PayrollRunLine(
                tenant_id=company.tenant_id,
                company_id=company.id,
                payroll_run_id=run.id,
                employee_id=employee.id,
                gross_pay=line_values["gross_pay"],
                wage_tax=line_values["wage_tax"],
                church_tax=line_values["church_tax"],
                solidarity_surcharge=line_values["solidarity_surcharge"],
                employee_social_security=line_values["employee_social_security"],
                employer_social_security=line_values["employer_social_security"],
                net_pay=line_values["net_pay"],
                employer_total=line_values["employer_total"],
                calculation=line_values["calculation"],
            )
        )
        totals["gross"] += line_values["gross_pay"]
        totals["wage_tax"] += line_values["wage_tax"]
        totals["church_tax"] += line_values["church_tax"]
        totals["solidarity"] += line_values["solidarity_surcharge"]
        totals["employee_sv"] += line_values["employee_social_security"]
        totals["employer_sv"] += line_values["employer_social_security"]
        totals["net"] += line_values["net_pay"]

    run.gross_total = totals["gross"]
    run.wage_tax_total = totals["wage_tax"]
    run.church_tax_total = totals["church_tax"]
    run.solidarity_surcharge_total = totals["solidarity"]
    run.employee_social_security_total = totals["employee_sv"]
    run.employer_social_security_total = totals["employer_sv"]
    run.net_total = totals["net"]
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="payroll_run",
        entity_id=str(run.id),
        action="created",
        changed_by=payload.changed_by,
        payload={
            "period_label": run.period_label,
            "payment_date": run.payment_date.isoformat(),
            "employee_count": len(employees),
            "gross_total": str(run.gross_total),
            "net_total": str(run.net_total),
        },
    )
    session.commit()
    return get_payroll_run(session=session, payroll_run_id=run.id)  # type: ignore[return-value]


def post_payroll_run(
    *, session: Session, payroll_run_id: int, changed_by: str = "system"
) -> PayrollRun:
    run = get_payroll_run(session=session, payroll_run_id=payroll_run_id)
    if run is None:
        raise PayrollError("Lohnlauf nicht gefunden.")
    if run.status == "posted":
        raise PayrollError("Lohnlauf ist bereits gebucht.")
    if not run.lines:
        raise PayrollError("Lohnlauf hat keine Zeilen.")

    journal_lines = _journal_lines_for_run(run)
    if len(journal_lines) < 2:
        raise PayrollError("Lohnlauf erzeugt keine buchbare Journalbuchung.")
    try:
        entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=run.company_id,
                entry_date=run.payment_date,
                description=f"Lohnlauf {run.period_label}",
                status="posted",
                source="payroll",
                changed_by=changed_by,
                lines=journal_lines,
            ),
        )
    except JournalEntryCreationError as exc:
        raise PayrollError(str(exc)) from exc

    run = get_payroll_run(session=session, payroll_run_id=payroll_run_id)
    if run is None:
        raise PayrollError("Lohnlauf nicht gefunden.")
    run.status = "posted"
    run.journal_entry_id = entry.id
    run.posted_at = datetime.now(timezone.utc)
    log_audit_event(
        session=session,
        tenant_id=run.tenant_id,
        company_id=run.company_id,
        entity_type="payroll_run",
        entity_id=str(run.id),
        action="posted",
        changed_by=changed_by,
        payload={
            "period_label": run.period_label,
            "journal_entry_id": entry.id,
            "gross_total": str(run.gross_total),
            "net_total": str(run.net_total),
        },
    )
    session.commit()
    return get_payroll_run(session=session, payroll_run_id=run.id)  # type: ignore[return-value]


def list_payroll_runs(*, session: Session, company_id: int) -> list[PayrollRun]:
    return (
        session.execute(
            select(PayrollRun)
            .where(PayrollRun.company_id == company_id)
            .options(
                selectinload(PayrollRun.lines).selectinload(PayrollRunLine.employee)
            )
            .order_by(PayrollRun.payment_date.desc(), PayrollRun.id.desc())
        )
        .scalars()
        .all()
    )


def get_payroll_run(*, session: Session, payroll_run_id: int) -> PayrollRun | None:
    return session.get(
        PayrollRun,
        payroll_run_id,
        options=[selectinload(PayrollRun.lines).selectinload(PayrollRunLine.employee)],
    )


def calculate_payroll_line(
    employee: PayrollEmployee,
    *,
    payment_date: date | None = None,
    period_label: str | None = None,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    gross = _money(employee.gross_monthly_salary)
    pap_result = None
    if config is not None and payment_date is not None and period_label is not None:
        try:
            pap_result = calculate_pap_wage_tax(
                employee=employee,
                gross_pay=gross,
                payment_date=payment_date,
                period_label=period_label,
                config=config,
            )
        except PayrollPapError as exc:
            raise PayrollError(str(exc)) from exc
    if pap_result is not None:
        wage_tax = _money(pap_result.wage_tax)
        church_tax = _money(pap_result.church_tax)
        solidarity = _money(pap_result.solidarity_surcharge)
        tax_calculation = {
            "mode": "pap_command",
            "version": pap_result.version,
            "protocol": pap_result.protocol,
        }
    else:
        wage_tax = _money(gross * Decimal(employee.wage_tax_rate))
        church_tax = _money(gross * Decimal(employee.church_tax_rate))
        solidarity = _money(gross * Decimal(employee.solidarity_surcharge_rate))
        tax_calculation = {
            "mode": "manual_rates",
            "wage_tax_rate": str(employee.wage_tax_rate),
            "church_tax_rate": str(employee.church_tax_rate),
            "solidarity_surcharge_rate": str(employee.solidarity_surcharge_rate),
        }
    employee_sv = _money(gross * Decimal(employee.employee_social_security_rate))
    employer_sv = _money(gross * Decimal(employee.employer_social_security_rate))
    net = _money(gross - wage_tax - church_tax - solidarity - employee_sv)
    if net < Decimal("0.00"):
        raise PayrollError(
            f"Abzüge für Mitarbeiter {employee.employee_number} übersteigen den Bruttolohn."
        )
    return {
        "gross_pay": gross,
        "wage_tax": wage_tax,
        "church_tax": church_tax,
        "solidarity_surcharge": solidarity,
        "employee_social_security": employee_sv,
        "employer_social_security": employer_sv,
        "net_pay": net,
        "employer_total": _money(gross + employer_sv),
        "calculation": {
            "tax": tax_calculation,
            "employee_social_security_rate": str(employee.employee_social_security_rate),
            "employer_social_security_rate": str(employee.employer_social_security_rate),
        },
    }


def decimal_from_payload(value: object, default: str = "0.00") -> Decimal:
    if value is None or value == "":
        value = default
    return parse_decimal(str(value))


def _payroll_employees_for_run(
    *,
    session: Session,
    company_id: int,
    payment_date: date,
    employee_ids: list[int] | None,
) -> list[PayrollEmployee]:
    stmt = (
        select(PayrollEmployee)
        .where(
            PayrollEmployee.company_id == company_id,
            PayrollEmployee.status == "active",
            PayrollEmployee.employment_start <= payment_date,
        )
        .order_by(PayrollEmployee.last_name, PayrollEmployee.first_name)
    )
    if employee_ids:
        stmt = stmt.where(PayrollEmployee.id.in_(employee_ids))
    employees = session.execute(stmt).scalars().all()
    return [
        employee
        for employee in employees
        if employee.employment_end is None or employee.employment_end >= payment_date
    ]


def _journal_lines_for_run(run: PayrollRun) -> list[JournalLineInput]:
    debit_totals: dict[int, Decimal] = {}
    credit_totals: dict[int, Decimal] = {}
    for line in run.lines:
        employee = line.employee
        _add(debit_totals, employee.wage_expense_account_id, line.gross_pay)
        _add(
            debit_totals,
            employee.employer_social_security_expense_account_id,
            line.employer_social_security,
        )
        _add(credit_totals, employee.payroll_liability_account_id, line.net_pay)
        _add(
            credit_totals,
            employee.wage_tax_liability_account_id,
            line.wage_tax + line.church_tax + line.solidarity_surcharge,
        )
        _add(
            credit_totals,
            employee.social_security_liability_account_id,
            line.employee_social_security + line.employer_social_security,
        )

    journal_lines = [
        JournalLineInput(
            account_id=account_id,
            debit_amount=amount,
            description=f"Lohnlauf {run.period_label}",
        )
        for account_id, amount in sorted(debit_totals.items())
        if amount > Decimal("0.00")
    ]
    journal_lines.extend(
        JournalLineInput(
            account_id=account_id,
            credit_amount=amount,
            description=f"Lohnlauf {run.period_label}",
        )
        for account_id, amount in sorted(credit_totals.items())
        if amount > Decimal("0.00")
    )
    return journal_lines


def _account_id(
    *,
    session: Session,
    company: Company,
    account_id: int | None,
    account_code: str | None,
    label: str,
) -> int:
    account = None
    if account_id is not None:
        account = session.get(Account, account_id)
    elif account_code and account_code.strip():
        account = session.execute(
            select(Account).where(
                Account.company_id == company.id,
                Account.code == account_code.strip(),
            )
        ).scalar_one_or_none()
    if account is None or account.company_id != company.id:
        raise PayrollError(f"{label} nicht gefunden.")
    if not account.is_active:
        raise PayrollError(f"{label} ist inaktiv.")
    return account.id


def _normalize_period(period_label: str) -> str:
    period_label = (period_label or "").strip()
    try:
        date.fromisoformat(f"{period_label}-01")
    except ValueError as exc:
        raise PayrollError("Zeitraum muss das Format JJJJ-MM haben.") from exc
    return period_label


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    value = Decimal(value)
    if value < Decimal("0.00"):
        raise PayrollError("Raten dürfen nicht negativ sein.")
    if value > Decimal("1.00"):
        raise PayrollError("Raten werden als Dezimalwert zwischen 0 und 1 erwartet.")
    return value


def _tax_class(value: int) -> int:
    value = int(value)
    if value < 1 or value > 6:
        raise PayrollError("Steuerklasse muss zwischen 1 und 6 liegen.")
    return value


def _add(target: dict[int, Decimal], account_id: int, amount: Decimal) -> None:
    target[account_id] = target.get(account_id, Decimal("0.00")) + _money(amount)
