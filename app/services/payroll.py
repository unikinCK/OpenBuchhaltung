"""Lohnbuchhaltung: Mitarbeiter, Lohnlauf und FiBu-Buchung.

Lohnsteuer, Solidaritätszuschlag und Kirchensteuer rechnet der Lohnlauf je Mitarbeiter
entweder nach dem amtlichen PAP des BMF (``tax_calculation = "pap"``) oder nach
hinterlegten Raten (``"manual"``). Sozialversicherungsbeiträge laufen über Raten;
ELStAM-Abruf und DEÜV-Meldungen sind nicht enthalten.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.controlling import ControllingError, validate_controlling_assignment
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.payroll_pap import (
    CHURCH_DENOMINATIONS,
    FEDERAL_STATES,
    PayrollPapError,
    calculate_builtin_wage_tax,
    calculate_pap_wage_tax,
)
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
    cost_center_id: int | None = None
    profit_center_id: int | None = None
    tax_calculation: str = "manual"
    church_denomination: str | None = None
    health_insurance_additional_rate: Decimal = Decimal("2.90")
    private_health_insurance: bool = False
    private_health_premium: Decimal = Decimal("0.00")
    private_health_employer_subsidy: Decimal = Decimal("0.00")
    care_insurance_children: int = 0
    pension_insurance_compulsory: bool = True
    unemployment_insurance_compulsory: bool = True
    monthly_tax_allowance: Decimal = Decimal("0.00")
    monthly_tax_addition: Decimal = Decimal("0.00")
    tax_factor: Decimal | None = None
    changed_by: str = "system"


TAX_CALCULATIONS = {"manual", "pap"}

# Felder, die ``update_payroll_employee`` ändern darf (Kontenfelder über *_account_id).
UPDATABLE_EMPLOYEE_FIELDS = (
    "first_name",
    "last_name",
    "employment_start",
    "employment_end",
    "status",
    "birth_date",
    "tax_id",
    "tax_class",
    "child_allowances",
    "federal_state",
    "main_employment",
    "social_security_number",
    "gross_monthly_salary",
    "wage_tax_rate",
    "church_tax_rate",
    "solidarity_surcharge_rate",
    "employee_social_security_rate",
    "employer_social_security_rate",
    "tax_calculation",
    "church_denomination",
    "health_insurance_additional_rate",
    "private_health_insurance",
    "private_health_premium",
    "private_health_employer_subsidy",
    "care_insurance_children",
    "pension_insurance_compulsory",
    "unemployment_insurance_compulsory",
    "monthly_tax_allowance",
    "monthly_tax_addition",
    "tax_factor",
    "cost_center_id",
    "profit_center_id",
    "wage_expense_account_id",
    "employer_social_security_expense_account_id",
    "payroll_liability_account_id",
    "wage_tax_liability_account_id",
    "social_security_liability_account_id",
)


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

    try:
        assignment_date = max(payload.employment_start, date.today())
        validate_controlling_assignment(
            session=session,
            company_id=company.id,
            entry_date=assignment_date,
            unit_id=payload.cost_center_id,
            expected_type="cost_center",
        )
        validate_controlling_assignment(
            session=session,
            company_id=company.id,
            entry_date=assignment_date,
            unit_id=payload.profit_center_id,
            expected_type="profit_center",
        )
    except ControllingError as exc:
        raise PayrollError(str(exc)) from exc

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
        federal_state=_federal_state(payload.federal_state),
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
        cost_center_id=payload.cost_center_id,
        profit_center_id=payload.profit_center_id,
        tax_calculation=_tax_calculation(payload.tax_calculation),
        church_denomination=_church_denomination(payload.church_denomination),
        health_insurance_additional_rate=_kv_additional_rate(
            payload.health_insurance_additional_rate
        ),
        private_health_insurance=bool(payload.private_health_insurance),
        private_health_premium=_non_negative_money(
            payload.private_health_premium, "PKV-Beitrag"
        ),
        private_health_employer_subsidy=_non_negative_money(
            payload.private_health_employer_subsidy, "Arbeitgeberzuschuss PKV"
        ),
        care_insurance_children=_care_children(payload.care_insurance_children),
        pension_insurance_compulsory=bool(payload.pension_insurance_compulsory),
        unemployment_insurance_compulsory=bool(payload.unemployment_insurance_compulsory),
        monthly_tax_allowance=_non_negative_money(
            payload.monthly_tax_allowance, "Freibetrag"
        ),
        monthly_tax_addition=_non_negative_money(
            payload.monthly_tax_addition, "Hinzurechnungsbetrag"
        ),
        tax_factor=_tax_factor(payload.tax_factor, payload.tax_class),
    )
    _validate_pap_employee(employee)
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
            "cost_center_id": employee.cost_center_id,
            "profit_center_id": employee.profit_center_id,
            "tax_calculation": employee.tax_calculation,
        },
    )
    session.commit()
    session.refresh(employee)
    return employee


def update_payroll_employee(
    *,
    session: Session,
    employee_id: int,
    changes: Mapping[str, object],
    changed_by: str = "system",
) -> PayrollEmployee:
    """Ändert Mitarbeiter-Stammdaten; Werte werden wie beim Anlegen geprüft.

    ``changes`` enthält nur die zu ändernden Felder (Python-Typen, z. B. ``date``,
    ``Decimal``). Bereits erstellte Lohnläufe bleiben unverändert.
    """
    employee = session.get(PayrollEmployee, employee_id)
    if employee is None:
        raise PayrollError("Mitarbeiter nicht gefunden.")
    unknown = sorted(set(changes) - set(UPDATABLE_EMPLOYEE_FIELDS))
    if unknown:
        raise PayrollError(f"Unbekannte Felder: {', '.join(unknown)}.")
    company = session.get(Company, employee.company_id)
    assert company is not None

    before = _employee_snapshot(employee)
    for field, value in changes.items():
        setattr(employee, field, _normalized_employee_value(session, company, field, value))

    if not (employee.first_name or "").strip() or not (employee.last_name or "").strip():
        raise PayrollError("Vor- und Nachname sind Pflicht.")
    if employee.gross_monthly_salary <= Decimal("0.00"):
        raise PayrollError("Bruttogehalt muss positiv sein.")
    if employee.employment_end and employee.employment_end < employee.employment_start:
        raise PayrollError("Austrittsdatum darf nicht vor Eintritt liegen.")
    employee.tax_factor = _tax_factor(employee.tax_factor, employee.tax_class)
    dimensions = (("cost_center_id", "cost_center"), ("profit_center_id", "profit_center"))
    for field, expected in dimensions:
        if field in changes:
            try:
                validate_controlling_assignment(
                    session=session,
                    company_id=company.id,
                    entry_date=max(employee.employment_start, date.today()),
                    unit_id=getattr(employee, field),
                    expected_type=expected,
                )
            except ControllingError as exc:
                raise PayrollError(str(exc)) from exc
    _validate_pap_employee(employee)

    after = _employee_snapshot(employee)
    changed = {
        key: {"before": before[key], "after": after[key]}
        for key in after
        if before[key] != after[key]
    }
    if changed:
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="payroll_employee",
            entity_id=str(employee.id),
            action="updated",
            changed_by=changed_by,
            payload={"employee_number": employee.employee_number, "changes": changed},
        )
    session.commit()
    session.refresh(employee)
    return employee


def calculate_wage_tax_preview(
    *, values: Mapping[str, object], period_label: str
) -> dict[str, object]:
    """Lohnsteuer eines Monatslohns nach dem amtlichen PAP, ohne etwas zu speichern.

    ``values`` nutzt dieselben Felder wie die Mitarbeiter-Stammdaten
    (``gross_monthly_salary``, ``tax_class``, ``birth_date``, ``federal_state`` …).
    """
    period_label = _normalize_period(period_label)
    if values.get("gross_monthly_salary") in (None, ""):
        raise PayrollError("Bruttolohn (gross_monthly_salary) ist Pflicht.")
    defaults: dict[str, object] = {
        "employee_number": "Probe",
        "tax_class": 1,
        "child_allowances": Decimal("0"),
        "church_denomination": None,
        "health_insurance_additional_rate": Decimal("2.90"),
        "private_health_insurance": False,
        "private_health_premium": Decimal("0"),
        "private_health_employer_subsidy": Decimal("0"),
        "care_insurance_children": 0,
        "pension_insurance_compulsory": True,
        "unemployment_insurance_compulsory": True,
        "monthly_tax_allowance": Decimal("0"),
        "monthly_tax_addition": Decimal("0"),
        "tax_factor": None,
    }
    employee = PayrollEmployee(**defaults)
    for field in (
        "birth_date",
        "tax_class",
        "child_allowances",
        "federal_state",
        "church_denomination",
        "health_insurance_additional_rate",
        "private_health_insurance",
        "private_health_premium",
        "private_health_employer_subsidy",
        "care_insurance_children",
        "pension_insurance_compulsory",
        "unemployment_insurance_compulsory",
        "monthly_tax_allowance",
        "monthly_tax_addition",
        "tax_factor",
    ):
        if field in values:
            setattr(employee, field, _preview_value(field, values[field]))
    employee.tax_factor = _tax_factor(employee.tax_factor, employee.tax_class)
    employee.tax_calculation = "pap"
    _validate_pap_employee(employee)
    gross = _money(Decimal(values["gross_monthly_salary"]))  # type: ignore[arg-type]
    if gross <= 0:
        raise PayrollError("Bruttolohn muss positiv sein.")
    try:
        result = calculate_builtin_wage_tax(
            employee=employee, gross_pay=gross, period_label=period_label
        )
    except PayrollPapError as exc:
        raise PayrollError(str(exc)) from exc
    total = result.wage_tax + result.solidarity_surcharge + result.church_tax
    return {
        "period_label": period_label,
        "gross_pay": str(gross),
        "wage_tax": str(_money(result.wage_tax)),
        "solidarity_surcharge": str(_money(result.solidarity_surcharge)),
        "church_tax": str(_money(result.church_tax)),
        "total_tax": str(_money(total)),
        "version": result.version,
        "pap": result.details,
        "notes": [
            "Berechnung nach dem amtlichen Programmablaufplan des BMF für laufenden "
            "Monatslohn; Sozialversicherungsbeiträge sind nicht enthalten.",
        ],
    }


def _preview_value(field: str, value: object) -> object:
    if field in {"birth_date"}:
        return value
    if field == "federal_state":
        return _federal_state(value)  # type: ignore[arg-type]
    if field == "church_denomination":
        return _church_denomination(value)  # type: ignore[arg-type]
    if field in {"tax_class"}:
        return _tax_class(int(value))  # type: ignore[call-overload]
    if field == "care_insurance_children":
        return _care_children(int(value or 0))  # type: ignore[call-overload]
    if field == "health_insurance_additional_rate":
        return _kv_additional_rate(Decimal(value))  # type: ignore[arg-type]
    if field in {
        "private_health_insurance",
        "pension_insurance_compulsory",
        "unemployment_insurance_compulsory",
    }:
        return bool(value)
    if field == "tax_factor":
        return Decimal(value) if value not in (None, "") else None  # type: ignore[arg-type]
    return Decimal(value or 0)  # type: ignore[arg-type]


def _employee_snapshot(employee: PayrollEmployee) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for field in UPDATABLE_EMPLOYEE_FIELDS:
        value = getattr(employee, field)
        if isinstance(value, (date, Decimal)):
            value = value.isoformat() if isinstance(value, date) else str(value)
        snapshot[field] = value
    return snapshot


def _normalized_employee_value(
    session: Session, company: Company, field: str, value: object
) -> object:
    if field.endswith("_account_id"):
        return _account_id(
            session=session,
            company=company,
            account_id=int(value) if value is not None else None,  # type: ignore[arg-type]
            account_code=None,
            label="Konto",
        )
    if field in {"first_name", "last_name"}:
        return str(value or "").strip()
    if field in {"tax_id", "social_security_number"}:
        return str(value or "").strip() or None
    if field == "status":
        if value not in {"active", "inactive"}:
            raise PayrollError("Status muss active oder inactive sein.")
        return value
    if field == "employment_start":
        if not isinstance(value, date):
            raise PayrollError("Eintrittsdatum ist Pflicht.")
        return value
    if field in {"employment_end", "birth_date"}:
        if value is not None and not isinstance(value, date):
            raise PayrollError("Ungültiges Datum.")
        return value
    if field == "tax_class":
        return _tax_class(int(value))  # type: ignore[call-overload]
    if field == "child_allowances":
        allowances = Decimal(value)  # type: ignore[arg-type]
        if allowances < 0:
            raise PayrollError("Kinderfreibeträge dürfen nicht negativ sein.")
        return allowances
    if field == "federal_state":
        return _federal_state(value)  # type: ignore[arg-type]
    if field in {
        "main_employment",
        "private_health_insurance",
        "pension_insurance_compulsory",
        "unemployment_insurance_compulsory",
    }:
        return bool(value)
    if field == "gross_monthly_salary":
        return _money(Decimal(value))  # type: ignore[arg-type]
    if field.endswith("_rate") and field != "health_insurance_additional_rate":
        return _rate(Decimal(value))  # type: ignore[arg-type]
    if field == "tax_calculation":
        return _tax_calculation(value)  # type: ignore[arg-type]
    if field == "church_denomination":
        return _church_denomination(value)  # type: ignore[arg-type]
    if field == "health_insurance_additional_rate":
        return _kv_additional_rate(Decimal(value))  # type: ignore[arg-type]
    if field in {
        "private_health_premium",
        "private_health_employer_subsidy",
        "monthly_tax_allowance",
        "monthly_tax_addition",
    }:
        return _non_negative_money(Decimal(value), field)  # type: ignore[arg-type]
    if field == "care_insurance_children":
        return _care_children(int(value))  # type: ignore[call-overload]
    if field == "tax_factor":
        return Decimal(value) if value not in (None, "") else None  # type: ignore[arg-type]
    if field in {"cost_center_id", "profit_center_id"}:
        return int(value) if value not in (None, "") else None  # type: ignore[call-overload]
    raise PayrollError(f"Feld {field} kann nicht geändert werden.")


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
                church_denomination=line_values["church_denomination"],
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
    # Der Lohnaufwand gehört periodengerecht in den Lohnmonat (Monatsende),
    # nicht auf das Zahlungsdatum. Buchung und Statuswechsel des Lohnlaufs
    # werden gemeinsam committet (commit=False), sonst bliebe bei einem
    # Fehler eine Buchung ohne "posted"-Lauf zurück, die ein erneuter
    # Versuch doppelt buchen würde.
    try:
        entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=run.company_id,
                entry_date=period_end_date(run.period_label),
                description=f"Lohnlauf {run.period_label}",
                status="posted",
                source="payroll",
                changed_by=changed_by,
                lines=journal_lines,
            ),
            commit=False,
        )
    except JournalEntryCreationError as exc:
        raise PayrollError(str(exc)) from exc

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
    mode = "manual_rates"
    try:
        if config is not None and payment_date is not None and period_label is not None:
            pap_result = calculate_pap_wage_tax(
                employee=employee,
                gross_pay=gross,
                payment_date=payment_date,
                period_label=period_label,
                config=config,
            )
            if pap_result is not None:
                mode = "pap_command"
        if pap_result is None and employee.tax_calculation == "pap":
            if period_label is None:
                raise PayrollError("Für die Lohnsteuer nach PAP fehlt der Lohnzeitraum.")
            pap_result = calculate_builtin_wage_tax(
                employee=employee, gross_pay=gross, period_label=_normalize_period(period_label)
            )
            mode = "pap"
    except PayrollPapError as exc:
        raise PayrollError(str(exc)) from exc
    if pap_result is not None:
        wage_tax = _money(pap_result.wage_tax)
        church_tax = _money(pap_result.church_tax)
        solidarity = _money(pap_result.solidarity_surcharge)
        tax_calculation = {
            "mode": mode,
            "version": pap_result.version,
            "protocol": pap_result.protocol,
        }
        if pap_result.details:
            tax_calculation["pap"] = pap_result.details
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
        "church_denomination": employee.church_denomination,
        "calculation": {
            "tax": tax_calculation,
            "employee_social_security_rate": str(employee.employee_social_security_rate),
            "employer_social_security_rate": str(employee.employer_social_security_rate),
        },
    }


def decimal_from_payload(value: object, default: str = "0.00") -> Decimal:
    """Dezimalwert aus Formular/JSON — ohne Cent-Prüfung, da auch Sätze
    (z. B. 0,195) und Kinderfreibeträge (0,5) darüber laufen; Geldbeträge
    werden im Service über ``_money`` auf Cent gerundet."""
    if value is None or value == "":
        value = default
    return parse_decimal(str(value), places=None)


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
    debit_totals: dict[tuple[int, int | None, int | None], Decimal] = {}
    credit_totals: dict[int, Decimal] = {}
    for line in run.lines:
        employee = line.employee
        dimension_key = (
            employee.cost_center_id,
            employee.profit_center_id,
        )
        _add_dimensioned(
            debit_totals,
            employee.wage_expense_account_id,
            *dimension_key,
            line.gross_pay,
        )
        _add_dimensioned(
            debit_totals,
            employee.employer_social_security_expense_account_id,
            *dimension_key,
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
            cost_center_id=cost_center_id,
            profit_center_id=profit_center_id,
        )
        for (account_id, cost_center_id, profit_center_id), amount in sorted(
            debit_totals.items(), key=lambda item: tuple(value or 0 for value in item[0])
        )
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


def period_end_date(period_label: str) -> date:
    """Letzter Tag des Lohnmonats ``JJJJ-MM`` (Buchungsdatum des Lohnaufwands)."""
    first_day = date.fromisoformat(f"{_normalize_period(period_label)}-01")
    return date(first_day.year, first_day.month, monthrange(first_day.year, first_day.month)[1])


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    value = Decimal(value)
    if value < Decimal("0.00"):
        raise PayrollError("Raten dürfen nicht negativ sein.")
    if value > Decimal("1.00"):
        raise PayrollError("Raten werden als Dezimalwert zwischen 0 und 1 erwartet.")
    return value


def _tax_calculation(value: str | None) -> str:
    value = (value or "manual").strip().lower()
    if value not in TAX_CALCULATIONS:
        raise PayrollError("Berechnungsart muss 'pap' oder 'manual' sein.")
    return value


def _church_denomination(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if not value or value in {"-", "--", "keine", "none"}:
        return None
    if value not in CHURCH_DENOMINATIONS:
        known = ", ".join(sorted(CHURCH_DENOMINATIONS))
        raise PayrollError(f"Unbekanntes Kirchensteuermerkmal {value!r} (bekannt: {known}).")
    return value


def _federal_state(value: str | None) -> str | None:
    value = (value or "").strip().upper()
    if not value:
        return None
    if value not in FEDERAL_STATES:
        raise PayrollError(f"Unbekanntes Bundesland-Kürzel {value!r}.")
    return value


def _kv_additional_rate(value: Decimal) -> Decimal:
    value = Decimal(value)
    if value < 0 or value > 10:
        raise PayrollError("Zusatzbeitragssatz wird in Prozent zwischen 0 und 10 erwartet.")
    return value.quantize(MONEY)


def _non_negative_money(value: Decimal, label: str) -> Decimal:
    value = _money(Decimal(value or 0))
    if value < 0:
        raise PayrollError(f"{label} darf nicht negativ sein.")
    return value


def _care_children(value: int) -> int:
    value = int(value or 0)
    if value < 0 or value > 30:
        raise PayrollError("Kinderzahl für die Pflegeversicherung ist ungültig.")
    return value


def _tax_factor(value: Decimal | None, tax_class: int) -> Decimal | None:
    if value in (None, ""):
        return None
    factor = Decimal(value)
    if int(tax_class) != 4:
        raise PayrollError("Ein Faktor ist nur in Steuerklasse IV möglich.")
    if factor <= 0 or factor >= 1:
        raise PayrollError("Der Faktor muss zwischen 0 und 1 liegen (z. B. 0,912).")
    if factor != factor.quantize(Decimal("0.001")):
        raise PayrollError("Der Faktor hat höchstens drei Nachkommastellen.")
    return factor


def _validate_pap_employee(employee: PayrollEmployee) -> None:
    if employee.tax_calculation != "pap":
        return
    missing = []
    if employee.birth_date is None:
        missing.append("Geburtsdatum")
    if not employee.federal_state:
        missing.append("Bundesland")
    if missing:
        raise PayrollError(
            "Für die Lohnsteuer nach PAP sind Pflicht: " + ", ".join(missing) + "."
        )


def _tax_class(value: int) -> int:
    value = int(value)
    if value < 1 or value > 6:
        raise PayrollError("Steuerklasse muss zwischen 1 und 6 liegen.")
    return value


def _add(target: dict[int, Decimal], account_id: int, amount: Decimal) -> None:
    target[account_id] = target.get(account_id, Decimal("0.00")) + _money(amount)


def _add_dimensioned(
    target: dict[tuple[int, int | None, int | None], Decimal],
    account_id: int,
    cost_center_id: int | None,
    profit_center_id: int | None,
    amount: Decimal,
) -> None:
    key = (account_id, cost_center_id, profit_center_id)
    target[key] = target.get(key, Decimal("0.00")) + _money(amount)
