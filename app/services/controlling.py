"""Kostenstellen- und Profitcenter-Stammdaten sowie Ergebnisberichte."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select, true
from sqlalchemy.orm import Session, aliased

from app.services.audit_log import log_audit_event, serialize_audit_log_entry
from domain.models import (
    Account,
    AuditLog,
    Company,
    ControllingUnit,
    DepreciationEntry,
    FixedAsset,
    JournalEntry,
    JournalEntryLine,
)

UNIT_TYPES = {"cost_center", "profit_center"}
_UNSET = object()


class ControllingError(ValueError):
    """Ungültige Controlling-Stammdaten oder Kontierung."""


def serialize_controlling_unit(unit: ControllingUnit) -> dict[str, Any]:
    return {
        "id": unit.id,
        "tenant_id": unit.tenant_id,
        "company_id": unit.company_id,
        "unit_type": unit.unit_type,
        "code": unit.code,
        "name": unit.name,
        "parent_id": unit.parent_id,
        "valid_from": unit.valid_from.isoformat() if unit.valid_from else None,
        "valid_to": unit.valid_to.isoformat() if unit.valid_to else None,
        "is_active": unit.is_active,
        "created_at": unit.created_at.isoformat() if unit.created_at else None,
    }


def _normalize_unit_type(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in UNIT_TYPES:
        raise ControllingError("unit_type must be cost_center or profit_center.")
    return normalized


def _validate_dates(valid_from: date | None, valid_to: date | None) -> None:
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        raise ControllingError("valid_from must not be after valid_to.")


def _validate_parent(
    *,
    session: Session,
    company_id: int,
    unit_type: str,
    parent_id: int | None,
    unit_id: int | None = None,
) -> ControllingUnit | None:
    if parent_id is None:
        return None
    parent = session.get(ControllingUnit, parent_id)
    if parent is None or parent.company_id != company_id or parent.unit_type != unit_type:
        raise ControllingError("Parent must belong to the same company and unit type.")
    if unit_id is not None:
        cursor: ControllingUnit | None = parent
        visited: set[int] = set()
        while cursor is not None:
            if cursor.id == unit_id:
                raise ControllingError("Parent assignment would create a hierarchy cycle.")
            if cursor.id in visited:
                raise ControllingError("Existing controlling hierarchy contains a cycle.")
            visited.add(cursor.id)
            cursor = session.get(ControllingUnit, cursor.parent_id) if cursor.parent_id else None
    return parent


def create_controlling_unit(
    *,
    session: Session,
    company: Company,
    unit_type: str,
    code: str,
    name: str,
    changed_by: str,
    parent_id: int | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    is_active: bool = True,
) -> ControllingUnit:
    normalized_type = _normalize_unit_type(unit_type)
    normalized_code = (code or "").strip()
    normalized_name = (name or "").strip()
    if not normalized_code:
        raise ControllingError("code is required.")
    if not normalized_name:
        raise ControllingError("name is required.")
    if not isinstance(is_active, bool):
        raise ControllingError("is_active must be a boolean.")
    _validate_dates(valid_from, valid_to)
    _validate_parent(
        session=session,
        company_id=company.id,
        unit_type=normalized_type,
        parent_id=parent_id,
    )

    unit = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type=normalized_type,
        code=normalized_code,
        name=normalized_name,
        parent_id=parent_id,
        valid_from=valid_from,
        valid_to=valid_to,
        is_active=is_active,
    )
    session.add(unit)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=unit.tenant_id,
        company_id=unit.company_id,
        entity_type="controlling_unit",
        entity_id=str(unit.id),
        action="created",
        changed_by=changed_by,
        payload={"before": None, "after": serialize_controlling_unit(unit)},
    )
    return unit


def update_controlling_unit(
    *,
    session: Session,
    unit: ControllingUnit,
    changed_by: str,
    name: str | object = _UNSET,
    parent_id: int | None | object = _UNSET,
    valid_from: date | None | object = _UNSET,
    valid_to: date | None | object = _UNSET,
    is_active: bool | object = _UNSET,
) -> bool:
    if all(value is _UNSET for value in (name, parent_id, valid_from, valid_to, is_active)):
        raise ControllingError(
            "At least one of name, parent_id, valid_from, valid_to or is_active is required."
        )
    before = serialize_controlling_unit(unit)

    if name is not _UNSET:
        if not isinstance(name, str):
            raise ControllingError("name must be a string.")
        normalized_name = name.strip()
        if not normalized_name:
            raise ControllingError("name must not be empty.")
        unit.name = normalized_name
    if parent_id is not _UNSET:
        _validate_parent(
            session=session,
            company_id=unit.company_id,
            unit_type=unit.unit_type,
            parent_id=parent_id,
            unit_id=unit.id,
        )
        unit.parent_id = parent_id
    if valid_from is not _UNSET:
        unit.valid_from = valid_from
    if valid_to is not _UNSET:
        unit.valid_to = valid_to
    if is_active is not _UNSET:
        if not isinstance(is_active, bool):
            raise ControllingError("is_active must be a boolean.")
        unit.is_active = is_active
    _validate_dates(unit.valid_from, unit.valid_to)

    after = serialize_controlling_unit(unit)
    if before == after:
        return False
    log_audit_event(
        session=session,
        tenant_id=unit.tenant_id,
        company_id=unit.company_id,
        entity_type="controlling_unit",
        entity_id=str(unit.id),
        action="updated",
        changed_by=changed_by,
        payload={"before": before, "after": after},
    )
    return True


def validate_controlling_assignment(
    *,
    session: Session,
    company_id: int,
    entry_date: date,
    unit_id: int | None,
    expected_type: str,
) -> ControllingUnit | None:
    if unit_id is None:
        return None
    unit = session.get(ControllingUnit, unit_id)
    if unit is None or unit.company_id != company_id or unit.unit_type != expected_type:
        label = "Kostenstelle" if expected_type == "cost_center" else "Profitcenter"
        raise ControllingError(f"{label} für Buchungszeile nicht gefunden.")
    if not unit.is_active:
        raise ControllingError(f"{unit.code} ist inaktiv und darf nicht kontiert werden.")
    if unit.valid_from and entry_date < unit.valid_from:
        raise ControllingError(f"{unit.code} ist am Buchungsdatum noch nicht gültig.")
    if unit.valid_to and entry_date > unit.valid_to:
        raise ControllingError(f"{unit.code} ist am Buchungsdatum nicht mehr gültig.")
    return unit


def controlling_unit_history(
    *, session: Session, unit_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 500))
    entries = (
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "controlling_unit",
                AuditLog.entity_id == str(unit_id),
            )
            .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
            .limit(bounded_limit)
        )
        .scalars()
        .all()
    )
    return [serialize_audit_log_entry(entry) for entry in entries]


def _empty_investments() -> dict[str, Any]:
    return {
        "accounts": [],
        "acquisition_cost": Decimal("0.00"),
        "depreciation": Decimal("0.00"),
        "book_value": Decimal("0.00"),
    }


def _investment_figures(
    *,
    session: Session,
    company_id: int,
    dimension_column,
    date_from: date | None,
    date_to: date | None,
) -> dict[int, dict[str, Any]]:
    """Investitionen je Einheit: Anschaffungswert, AfA und Buchwert je Anlagekonto.

    Anschaffungswert = kontierte Zeilen auf Bestandskonten (Aktiva) bis zum Ende des
    Berichtszeitraums, ohne Abschreibungsbuchungen. Abschreibungen (planmäßig,
    außerplanmäßig, Restbuchwert beim Abgang) stammen aus der Anlagenbuchhaltung: Die
    Einheit trägt dort die Aufwandszeile, die Minderung des Anlagekontos nicht – das
    Anlagekonto liefert daher das Anlagegut. Stornos zählen über ``reversal_of_id``
    mit. AfA im Zeitraum, Buchwert = Anschaffungswert − kumulierte AfA zum Stichtag.
    """
    depreciation_entry_ids = select(DepreciationEntry.journal_entry_id).where(
        DepreciationEntry.company_id == company_id,
        DepreciationEntry.journal_entry_id.is_not(None),
    )
    cost_stmt = (
        select(
            dimension_column.label("unit_id"),
            Account.id.label("account_id"),
            Account.code.label("account_code"),
            Account.name.label("account_name"),
            func.coalesce(
                func.sum(JournalEntryLine.debit_amount - JournalEntryLine.credit_amount), 0
            ).label("amount"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(
            JournalEntry.company_id == company_id,
            Account.account_type == "asset",
            dimension_column.is_not(None),
            JournalEntry.id.not_in(depreciation_entry_ids),
            func.coalesce(JournalEntry.reversal_of_id, 0).not_in(depreciation_entry_ids),
        )
        .group_by(dimension_column, Account.id, Account.code, Account.name)
    )

    asset_account = aliased(Account)
    expense_account = aliased(Account)
    in_period = JournalEntry.entry_date >= date_from if date_from is not None else true()
    line_amount = JournalEntryLine.debit_amount - JournalEntryLine.credit_amount
    depreciation_stmt = (
        select(
            dimension_column.label("unit_id"),
            asset_account.id.label("account_id"),
            asset_account.code.label("account_code"),
            asset_account.name.label("account_name"),
            func.coalesce(func.sum(line_amount), 0).label("cumulative"),
            func.coalesce(
                func.sum(case((in_period, line_amount), else_=0)), 0
            ).label("in_period"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(
            DepreciationEntry,
            DepreciationEntry.journal_entry_id
            == func.coalesce(JournalEntry.reversal_of_id, JournalEntry.id),
        )
        .join(FixedAsset, FixedAsset.id == DepreciationEntry.fixed_asset_id)
        .join(asset_account, asset_account.id == FixedAsset.asset_account_id)
        .join(expense_account, expense_account.id == JournalEntryLine.account_id)
        .where(
            JournalEntry.company_id == company_id,
            expense_account.account_type != "asset",
            dimension_column.is_not(None),
        )
        .group_by(dimension_column, asset_account.id, asset_account.code, asset_account.name)
    )
    if date_to is not None:
        cost_stmt = cost_stmt.where(JournalEntry.entry_date <= date_to)
        depreciation_stmt = depreciation_stmt.where(JournalEntry.entry_date <= date_to)

    rows: dict[int, dict[int, dict[str, Any]]] = {}

    def account_row(unit_id: int, row) -> dict[str, Any]:
        return rows.setdefault(unit_id, {}).setdefault(
            row.account_id,
            {
                "account_id": row.account_id,
                "code": row.account_code,
                "name": row.account_name,
                "acquisition_cost": Decimal("0.00"),
                "depreciation": Decimal("0.00"),
                "accumulated_depreciation": Decimal("0.00"),
            },
        )

    for row in session.execute(cost_stmt):
        account_row(row.unit_id, row)["acquisition_cost"] += Decimal(row.amount)
    for row in session.execute(depreciation_stmt):
        target = account_row(row.unit_id, row)
        target["depreciation"] += Decimal(row.in_period)
        target["accumulated_depreciation"] += Decimal(row.cumulative)

    result: dict[int, dict[str, Any]] = {}
    for unit_id, accounts in rows.items():
        investments = _empty_investments()
        for account in sorted(accounts.values(), key=lambda item: item["code"]):
            accumulated = account.pop("accumulated_depreciation")
            account["book_value"] = account["acquisition_cost"] - accumulated
            investments["accounts"].append(account)
            for key in ("acquisition_cost", "depreciation", "book_value"):
                investments[key] += account[key]
        result[unit_id] = investments
    return result


def controlling_result_report(
    *,
    session: Session,
    company_id: int,
    unit_type: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    # Lokal importiert: reports → journal_entries → controlling wäre sonst zirkulär.
    from app.services.reports import REVENUE_ACCOUNT_TYPES

    normalized_type = _normalize_unit_type(unit_type)
    units = (
        session.execute(
            select(ControllingUnit)
            .where(
                ControllingUnit.company_id == company_id,
                ControllingUnit.unit_type == normalized_type,
            )
            .order_by(ControllingUnit.code)
        )
        .scalars()
        .all()
    )
    dimension_column = (
        JournalEntryLine.cost_center_id
        if normalized_type == "cost_center"
        else JournalEntryLine.profit_center_id
    )
    unit_alias = aliased(ControllingUnit)
    stmt = (
        select(
            dimension_column.label("unit_id"),
            Account.id.label("account_id"),
            Account.code.label("account_code"),
            Account.name.label("account_name"),
            Account.account_type,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0).label("debit_total"),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0).label("credit_total"),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .outerjoin(unit_alias, unit_alias.id == dimension_column)
        .where(
            JournalEntry.company_id == company_id,
            Account.account_type.in_([*sorted(REVENUE_ACCOUNT_TYPES), "expense"]),
        )
        .group_by(
            dimension_column,
            Account.id,
            Account.code,
            Account.name,
            Account.account_type,
        )
        .order_by(dimension_column, Account.code)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)

    buckets: dict[int | None, dict[str, Any]] = {
        unit.id: {
            "unit": serialize_controlling_unit(unit),
            "accounts": [],
            "total_revenue": Decimal("0.00"),
            "total_expense": Decimal("0.00"),
            "net_income": Decimal("0.00"),
        }
        for unit in units
    }
    buckets[None] = {
        "unit": None,
        "accounts": [],
        "total_revenue": Decimal("0.00"),
        "total_expense": Decimal("0.00"),
        "net_income": Decimal("0.00"),
    }
    for row in session.execute(stmt):
        bucket = buckets.setdefault(
            row.unit_id,
            {
                "unit": None,
                "accounts": [],
                "total_revenue": Decimal("0.00"),
                "total_expense": Decimal("0.00"),
                "net_income": Decimal("0.00"),
            },
        )
        debit = Decimal(row.debit_total)
        credit = Decimal(row.credit_total)
        is_revenue = row.account_type in REVENUE_ACCOUNT_TYPES
        amount = credit - debit if is_revenue else debit - credit
        bucket["accounts"].append(
            {
                "account_id": row.account_id,
                "code": row.account_code,
                "name": row.account_name,
                "account_type": row.account_type,
                "amount": amount,
            }
        )
        if is_revenue:
            bucket["total_revenue"] += amount
        else:
            bucket["total_expense"] += amount
        bucket["net_income"] = bucket["total_revenue"] - bucket["total_expense"]

    investments = _investment_figures(
        session=session,
        company_id=company_id,
        dimension_column=dimension_column,
        date_from=date_from,
        date_to=date_to,
    )
    for unit_id, bucket in buckets.items():
        if unit_id is not None:
            bucket["investments"] = investments.get(unit_id) or _empty_investments()
    # Ohne Einheit lassen sich Bestandskonten nicht sinnvoll als Investition lesen.
    buckets[None]["investments"] = None

    return {
        "company_id": company_id,
        "unit_type": normalized_type,
        "period": {
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
        "units": [buckets[unit.id] for unit in units],
        "unassigned": buckets[None],
    }


# Filterwert „ohne Einheit“ für die Buchungssuche (entspricht „Nicht zugeordnet“).
UNASSIGNED_UNIT = "none"
_UNIT_COLUMNS = {
    "cost_center": JournalEntryLine.cost_center_id,
    "profit_center": JournalEntryLine.profit_center_id,
}


def parse_unit_filter(raw: str | None, *, unit_type: str) -> int | str | None:
    """Filterwert für Kostenstelle/Profitcenter: ID, ``none`` oder leer."""
    normalized = (raw or "").strip().lower()
    if not normalized:
        return None
    if normalized == UNASSIGNED_UNIT:
        return UNASSIGNED_UNIT
    if normalized.isdigit():
        return int(normalized)
    raise ControllingError(f"{unit_type}_id must be an integer or '{UNASSIGNED_UNIT}'.")


def find_filter_unit(
    session: Session, *, company_id: int, unit_type: str, unit_id: int
) -> ControllingUnit | None:
    """Einheit der Gesellschaft mit passendem Typ (auch inaktiv) oder None."""
    unit = session.get(ControllingUnit, unit_id)
    if unit is None or unit.company_id != company_id or unit.unit_type != unit_type:
        return None
    return unit


def journal_line_filter(
    *,
    account_id: int | None = None,
    cost_center: int | str | None = None,
    profit_center: int | str | None = None,
):
    """Buchungs-IDs mit mindestens einer Zeile, die alle Merkmale zugleich trägt.

    Konto und Einheiten müssen auf derselben Zeile stehen – so entspricht der
    Treffer genau dem Betrag im Controlling-Bericht. ``none`` sucht wie der
    Bericht nur Erfolgszeilen (Erlös/Aufwand) ohne Einheit. Ohne Kriterium None.
    """
    # Lokal importiert: reports → journal_entries → controlling wäre sonst zirkulär.
    from app.services.reports import REVENUE_ACCOUNT_TYPES

    conditions = []
    result_lines_only = False
    if account_id is not None:
        conditions.append(JournalEntryLine.account_id == account_id)
    for unit_type, value in (("cost_center", cost_center), ("profit_center", profit_center)):
        column = _UNIT_COLUMNS[unit_type]
        if value == UNASSIGNED_UNIT:
            conditions.append(column.is_(None))
            result_lines_only = True
        elif value is not None:
            conditions.append(column == value)
    if not conditions:
        return None
    stmt = select(JournalEntryLine.journal_entry_id)
    if result_lines_only:
        stmt = stmt.join(Account, Account.id == JournalEntryLine.account_id)
        conditions.append(
            Account.account_type.in_([*sorted(REVENUE_ACCOUNT_TYPES), "expense"])
        )
    return stmt.where(*conditions)
