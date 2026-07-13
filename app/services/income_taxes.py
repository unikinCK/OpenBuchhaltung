"""Körperschaftsteuer und Gewerbesteuer: Berechnung und Snapshots.

Die Berechnung nutzt das handelsrechtliche Jahresergebnis aus der GuV als
Startpunkt. Steuerliche Korrekturen werden bewusst strukturiert als Eingabe
geführt; OpenBuchhaltung versucht nicht, komplexe Hinzurechnungen/Kürzungen aus
Kontennamen zu erraten.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.reports import income_statement_for_company
from domain.models import Company, FiscalYear, IncomeTaxReturn

ZERO = Decimal("0.00")

TAX_TYPE_CORPORATE_INCOME = "corporate_income"
TAX_TYPE_TRADE_TAX = "trade_tax"
INCOME_TAX_TYPES = {TAX_TYPE_CORPORATE_INCOME, TAX_TYPE_TRADE_TAX}

DECLARATION_TYPE_DECLARATION = "declaration"
DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT = "prepayment_adjustment"
DECLARATION_TYPES = {
    DECLARATION_TYPE_DECLARATION,
    DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT,
}

CORPORATE_TAX_RATE = Decimal("15.00")
SOLIDARITY_SURCHARGE_RATE = Decimal("5.50")
TRADE_TAX_BASE_RATE = Decimal("3.50")


class IncomeTaxError(ValueError):
    """Raised when an income tax calculation cannot be fulfilled."""


class IncomeTaxConflict(IncomeTaxError):
    """Raised when the requested immutable snapshot already exists."""


@dataclass(frozen=True, slots=True)
class TaxAdjustment:
    code: str
    label: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class IncomeTaxPeriod:
    fiscal_year_id: int | None
    label: str
    date_from: date
    date_to: date


def year_bounds(year: int | str) -> tuple[date, date, str]:
    """Return calendar-year bounds for compatibility and validation helpers."""
    parsed_year = _parse_year(year)
    return date(parsed_year, 1, 1), date(parsed_year, 12, 31), str(parsed_year)


def _parse_year(year: int | str | None) -> int:
    try:
        parsed_year = int(str(year).strip())
        if parsed_year < 1900 or parsed_year > 2999:
            raise ValueError
    except (TypeError, ValueError):
        raise IncomeTaxError("year must be a four digit calendar year.") from None
    return parsed_year


def resolve_income_tax_period(
    *,
    session: Session,
    company: Company,
    year: int | str | None,
    fiscal_year_id: int | None = None,
) -> IncomeTaxPeriod:
    """Resolve the exact accounting period used as the tax calculation basis."""
    if fiscal_year_id is not None:
        fiscal_year = session.get(FiscalYear, fiscal_year_id)
        if fiscal_year is None or fiscal_year.company_id != company.id:
            raise IncomeTaxError("Wirtschaftsjahr nicht gefunden.")
        return IncomeTaxPeriod(
            fiscal_year_id=fiscal_year.id,
            label=fiscal_year.label,
            date_from=fiscal_year.start_date,
            date_to=fiscal_year.end_date,
        )

    parsed_year = _parse_year(year)
    fiscal_years = (
        session.execute(
            select(FiscalYear)
            .where(
                FiscalYear.company_id == company.id,
                FiscalYear.end_date >= date(parsed_year, 1, 1),
                FiscalYear.end_date <= date(parsed_year, 12, 31),
            )
            .order_by(FiscalYear.start_date)
        )
        .scalars()
        .all()
    )
    if len(fiscal_years) > 1:
        raise IncomeTaxError(
            "Mehrere Wirtschaftsjahre enden im angegebenen Jahr; fiscal_year_id ist erforderlich."
        )
    if fiscal_years:
        fiscal_year = fiscal_years[0]
        return IncomeTaxPeriod(
            fiscal_year_id=fiscal_year.id,
            label=fiscal_year.label,
            date_from=fiscal_year.start_date,
            date_to=fiscal_year.end_date,
        )

    start_month = company.fiscal_year_start_month
    if start_month == 1:
        date_from, date_to, label = year_bounds(parsed_year)
    else:
        start_year = parsed_year - 1
        end_month = start_month - 1
        date_from = date(start_year, start_month, 1)
        date_to = date(parsed_year, end_month, calendar.monthrange(parsed_year, end_month)[1])
        label = f"{start_year}/{parsed_year}"
    return IncomeTaxPeriod(
        fiscal_year_id=None,
        label=label,
        date_from=date_from,
        date_to=date_to,
    )


def normalize_adjustments(items: Iterable[dict[str, object]] | None) -> list[TaxAdjustment]:
    result: list[TaxAdjustment] = []
    for index, item in enumerate(items or [], start=1):
        amount = _decimal(item.get("amount", ZERO))
        if amount < ZERO:
            raise IncomeTaxError("adjustment amounts must be non-negative.")
        code = str(item.get("code") or f"ADJ{index:03d}").strip()
        label = str(item.get("label") or item.get("description") or code).strip()
        if not code or not label:
            raise IncomeTaxError("adjustments require code and label.")
        result.append(TaxAdjustment(code=code, label=label, amount=_money(amount)))
    return result


def compute_income_tax_return(
    *,
    session: Session,
    company_id: int,
    year: int | str | None,
    tax_type: str,
    declaration_type: str = DECLARATION_TYPE_DECLARATION,
    fiscal_year_id: int | None = None,
    additions: Iterable[dict[str, object]] | None = None,
    reductions: Iterable[dict[str, object]] | None = None,
    loss_carryforward: Decimal | str | int = ZERO,
    prepayments: Decimal | str | int = ZERO,
    municipality_multiplier: Decimal | str | int | None = None,
    trade_tax_allowance: Decimal | str | int = ZERO,
) -> dict[str, object]:
    tax_type = _normalize_tax_type(tax_type)
    declaration_type = _normalize_declaration_type(declaration_type)
    company = session.get(Company, company_id)
    if company is None:
        raise IncomeTaxError("Gesellschaft nicht gefunden.")
    period = resolve_income_tax_period(
        session=session,
        company=company,
        year=year,
        fiscal_year_id=fiscal_year_id,
    )

    income_statement = income_statement_for_company(
        session=session,
        company_id=company_id,
        date_from=period.date_from,
        date_to=period.date_to,
    )
    net_income = _money(income_statement["totals"]["net_income"])
    normalized_additions = normalize_adjustments(additions)
    normalized_reductions = normalize_adjustments(reductions)
    additions_total = _sum_adjustments(normalized_additions)
    reductions_total = _sum_adjustments(normalized_reductions)
    loss = _money(_decimal(loss_carryforward))
    paid = _money(_decimal(prepayments))
    if loss < ZERO or paid < ZERO:
        raise IncomeTaxError("loss_carryforward and prepayments must be non-negative.")

    taxable_before_floor = net_income + additions_total - reductions_total - loss
    taxable_income = _money(max(taxable_before_floor, ZERO))

    if tax_type == TAX_TYPE_CORPORATE_INCOME:
        result = _compute_corporate_income_tax(
            taxable_income=taxable_income, prepayments=paid
        )
    else:
        result = _compute_trade_tax(
            taxable_income=taxable_income,
            prepayments=paid,
            municipality_multiplier=municipality_multiplier,
            trade_tax_allowance=trade_tax_allowance,
        )

    return {
        "company_id": company_id,
        "fiscal_year_id": period.fiscal_year_id,
        "tax_type": tax_type,
        "declaration_type": declaration_type,
        "period_label": period.label,
        "date_from": period.date_from.isoformat(),
        "date_to": period.date_to.isoformat(),
        "basis": {
            "net_income": str(net_income),
            "additions": [_adjustment_payload(item) for item in normalized_additions],
            "additions_total": str(additions_total),
            "reductions": [_adjustment_payload(item) for item in normalized_reductions],
            "reductions_total": str(reductions_total),
            "loss_carryforward": str(loss),
            "taxable_income_before_floor": str(_money(taxable_before_floor)),
            "taxable_income": str(taxable_income),
        },
        "calculation": result,
        "legal_basis": _legal_basis(tax_type),
        "warnings": _warnings(tax_type, municipality_multiplier),
    }


def save_income_tax_return(
    *,
    session: Session,
    company_id: int,
    year: int | str | None,
    tax_type: str,
    declaration_type: str,
    changed_by: str,
    fiscal_year_id: int | None = None,
    additions: Iterable[dict[str, object]] | None = None,
    reductions: Iterable[dict[str, object]] | None = None,
    loss_carryforward: Decimal | str | int = ZERO,
    prepayments: Decimal | str | int = ZERO,
    municipality_multiplier: Decimal | str | int | None = None,
    trade_tax_allowance: Decimal | str | int = ZERO,
) -> IncomeTaxReturn:
    tax_type = _normalize_tax_type(tax_type)
    declaration_type = _normalize_declaration_type(declaration_type)
    company = session.get(Company, company_id)
    if company is None:
        raise IncomeTaxError("Gesellschaft nicht gefunden.")
    period = resolve_income_tax_period(
        session=session,
        company=company,
        year=year,
        fiscal_year_id=fiscal_year_id,
    )

    existing = session.execute(
        select(IncomeTaxReturn.id).where(
            IncomeTaxReturn.company_id == company_id,
            IncomeTaxReturn.tax_type == tax_type,
            IncomeTaxReturn.declaration_type == declaration_type,
            IncomeTaxReturn.period_label == period.label,
        )
    ).first()
    if existing:
        raise IncomeTaxConflict(
            f"Für {display_tax_type(tax_type)} {display_declaration_type(declaration_type)} "
            f"{period.label} existiert bereits ein Snapshot."
        )

    calculation = compute_income_tax_return(
        session=session,
        company_id=company_id,
        year=year,
        tax_type=tax_type,
        declaration_type=declaration_type,
        fiscal_year_id=period.fiscal_year_id,
        additions=additions,
        reductions=reductions,
        loss_carryforward=loss_carryforward,
        prepayments=prepayments,
        municipality_multiplier=municipality_multiplier,
        trade_tax_allowance=trade_tax_allowance,
    )
    item = IncomeTaxReturn(
        tenant_id=company.tenant_id,
        company_id=company.id,
        fiscal_year_id=period.fiscal_year_id,
        tax_type=tax_type,
        declaration_type=declaration_type,
        period_label=period.label,
        date_from=period.date_from,
        date_to=period.date_to,
        calculation=calculation,
        status="erstellt",
        created_by=changed_by,
    )
    session.add(item)
    try:
        session.flush()
        log_audit_event(
            session=session,
            tenant_id=company.tenant_id,
            company_id=company.id,
            entity_type="income_tax_return",
            entity_id=str(item.id),
            action="created",
            changed_by=changed_by,
            payload={
                "tax_type": tax_type,
                "declaration_type": declaration_type,
                "period_label": period.label,
                "fiscal_year_id": period.fiscal_year_id,
                "calculation": calculation,
            },
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise IncomeTaxConflict(
            f"Für {display_tax_type(tax_type)} {display_declaration_type(declaration_type)} "
            f"{period.label} existiert bereits ein Snapshot."
        ) from exc
    session.refresh(item)
    return item


def list_income_tax_returns(
    *,
    session: Session,
    company_id: int,
    tax_type: str | None = None,
) -> list[IncomeTaxReturn]:
    stmt = (
        select(IncomeTaxReturn)
        .where(IncomeTaxReturn.company_id == company_id)
        .order_by(IncomeTaxReturn.date_to.desc(), IncomeTaxReturn.id.desc())
    )
    if tax_type:
        stmt = stmt.where(IncomeTaxReturn.tax_type == _normalize_tax_type(tax_type))
    return session.execute(stmt).scalars().all()


def display_tax_type(tax_type: str) -> str:
    return {
        TAX_TYPE_CORPORATE_INCOME: "Körperschaftsteuer",
        TAX_TYPE_TRADE_TAX: "Gewerbesteuer",
    }[_normalize_tax_type(tax_type)]


def display_declaration_type(declaration_type: str) -> str:
    return {
        DECLARATION_TYPE_DECLARATION: "Erklärung",
        DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT: "Vorauszahlungsanpassung",
    }[_normalize_declaration_type(declaration_type)]


def _compute_corporate_income_tax(
    *, taxable_income: Decimal, prepayments: Decimal
) -> dict[str, object]:
    corporate_tax = _money(taxable_income * CORPORATE_TAX_RATE / Decimal("100"))
    solidarity = _money(corporate_tax * SOLIDARITY_SURCHARGE_RATE / Decimal("100"))
    total = _money(corporate_tax + solidarity)
    return {
        "rates": {
            "corporate_tax_percent": str(CORPORATE_TAX_RATE),
            "solidarity_surcharge_percent": str(SOLIDARITY_SURCHARGE_RATE),
        },
        "rows": [
            _row("taxable_income", "Zu versteuerndes Einkommen", taxable_income),
            _row("corporate_tax", "Körperschaftsteuer", corporate_tax),
            _row("solidarity_surcharge", "Solidaritätszuschlag", solidarity),
            _row("tax_total", "Steuer gesamt", total),
            _row("prepayments", "Vorauszahlungen", prepayments),
            _row("payable", "Zahllast / Erstattung", total - prepayments),
        ],
        "tax_total": str(total),
        "payable": str(_money(total - prepayments)),
    }


def _compute_trade_tax(
    *,
    taxable_income: Decimal,
    prepayments: Decimal,
    municipality_multiplier: Decimal | str | int | None,
    trade_tax_allowance: Decimal | str | int,
) -> dict[str, object]:
    multiplier = _decimal(municipality_multiplier if municipality_multiplier is not None else 400)
    allowance = _money(_decimal(trade_tax_allowance))
    if multiplier < ZERO:
        raise IncomeTaxError("municipality_multiplier must be non-negative.")
    if allowance < ZERO:
        raise IncomeTaxError("trade_tax_allowance must be non-negative.")
    rounded_trade_income = _round_down_hundred(taxable_income)
    taxable_trade_income = max(rounded_trade_income - allowance, ZERO)
    tax_base_amount = _money(taxable_trade_income * TRADE_TAX_BASE_RATE / Decimal("100"))
    trade_tax = _money(tax_base_amount * multiplier / Decimal("100"))
    return {
        "rates": {
            "trade_tax_base_rate_percent": str(TRADE_TAX_BASE_RATE),
            "municipality_multiplier_percent": str(_money(multiplier)),
        },
        "rows": [
            _row("trade_income_before_rounding", "Gewerbeertrag vor Abrundung", taxable_income),
            _row("rounded_trade_income", "Gewerbeertrag abgerundet", rounded_trade_income),
            _row("allowance", "Freibetrag", allowance),
            _row("taxable_trade_income", "Gekürzter Gewerbeertrag", taxable_trade_income),
            _row("tax_base_amount", "Gewerbesteuermessbetrag", tax_base_amount),
            _row("trade_tax", "Gewerbesteuer", trade_tax),
            _row("prepayments", "Vorauszahlungen", prepayments),
            _row("payable", "Zahllast / Erstattung", trade_tax - prepayments),
        ],
        "tax_total": str(trade_tax),
        "payable": str(_money(trade_tax - prepayments)),
    }


def _normalize_tax_type(tax_type: str) -> str:
    normalized = tax_type.strip().lower()
    aliases = {
        "kst": TAX_TYPE_CORPORATE_INCOME,
        "corporate": TAX_TYPE_CORPORATE_INCOME,
        "corporate_income": TAX_TYPE_CORPORATE_INCOME,
        "koerperschaftsteuer": TAX_TYPE_CORPORATE_INCOME,
        "körperschaftsteuer": TAX_TYPE_CORPORATE_INCOME,
        "gewst": TAX_TYPE_TRADE_TAX,
        "trade": TAX_TYPE_TRADE_TAX,
        "trade_tax": TAX_TYPE_TRADE_TAX,
        "gewerbesteuer": TAX_TYPE_TRADE_TAX,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in INCOME_TAX_TYPES:
        raise IncomeTaxError("tax_type must be 'corporate_income' or 'trade_tax'.")
    return normalized


def _normalize_declaration_type(declaration_type: str) -> str:
    normalized = declaration_type.strip().lower()
    aliases = {
        "erklaerung": DECLARATION_TYPE_DECLARATION,
        "erklärung": DECLARATION_TYPE_DECLARATION,
        "declaration": DECLARATION_TYPE_DECLARATION,
        "vorauszahlung": DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT,
        "vorauszahlungsanpassung": DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT,
        "prepayment": DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT,
        "prepayment_adjustment": DECLARATION_TYPE_PREPAYMENT_ADJUSTMENT,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in DECLARATION_TYPES:
        raise IncomeTaxError(
            "declaration_type must be 'declaration' or 'prepayment_adjustment'."
        )
    return normalized


def _legal_basis(tax_type: str) -> dict[str, object]:
    if tax_type == TAX_TYPE_CORPORATE_INCOME:
        return {
            "summary": "KSt-Schaetzung mit 15 % KSt und 5,5 % SolZ auf KSt.",
            "references": ["KStG § 23", "SolZG § 4"],
        }
    return {
        "summary": (
            "GewSt-Schaetzung: Gewerbeertrag +/- Korrekturen, Abrundung auf volle "
            "100 EUR, Messzahl 3,5 %, Hebesatz der Gemeinde."
        ),
        "references": ["GewStG §§ 8, 9", "GewStG § 11", "GewStG § 16"],
    }


def _warnings(tax_type: str, municipality_multiplier: Decimal | str | int | None) -> list[str]:
    warnings: list[str] = []
    if tax_type == TAX_TYPE_TRADE_TAX and municipality_multiplier is None:
        warnings.append(
            "Kein Hebesatz uebergeben; Berechnung verwendet 400 % als Arbeitsannahme."
        )
    warnings.append(
        "Steuerliche Korrekturen werden nur aus den uebergebenen Hinzurechnungen/"
        "Kuerzungen gebildet."
    )
    return warnings


def _adjustment_payload(item: TaxAdjustment) -> dict[str, str]:
    return {"code": item.code, "label": item.label, "amount": str(item.amount)}


def _sum_adjustments(items: list[TaxAdjustment]) -> Decimal:
    return _money(sum((item.amount for item in items), ZERO))


def _row(code: str, label: str, amount: Decimal) -> dict[str, str]:
    return {"code": code, "label": label, "amount": str(_money(amount))}


def _decimal(value: Decimal | str | int | float | object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        raise IncomeTaxError(f"Invalid decimal amount: {value!r}.") from None


def _money(value: Decimal) -> Decimal:
    return value.quantize(ZERO)


def _round_down_hundred(value: Decimal) -> Decimal:
    if value <= ZERO:
        return ZERO
    return (value / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN) * Decimal(
        "100"
    )
