"""Kontoblatt: Buchungen eines Sachkontos mit Anfangssaldo und laufendem Saldo.

Salden sind Soll minus Haben (positiv = Sollsaldo), wie in der
Summen-/Saldenliste. Saldovorträge (EB-Werte aus dem Jahresabschluss) erscheinen
nicht als Zeilen – sie wiederholen nur die Vorjahre, die der Anfangssaldo
bereits kumuliert enthält. Erfolgskonten beginnen je Geschäftsjahr bei null.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.services.accounts import serialize_account
from app.services.journal_entries import SOURCE_CARRYFORWARD
from app.services.partners import partner_display_number
from app.services.reports import BALANCE_SHEET_ACCOUNT_TYPES, fiscal_year_for_date
from domain.models import (
    Account,
    BusinessPartner,
    ControllingUnit,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
)

ZERO = Decimal("0.00")
DEFAULT_LEDGER_LIMIT = 200
MAX_LEDGER_LIMIT = 1000


class AccountLedgerError(ValueError):
    """Ungültige Parameter für das Kontoblatt."""


@dataclass(frozen=True)
class LedgerRange:
    date_from: date | None
    date_to: date | None
    fiscal_year: FiscalYear | None


def resolve_ledger_range(
    *,
    session: Session,
    account: Account,
    date_from: date | None = None,
    date_to: date | None = None,
    fiscal_year_id: int | None = None,
) -> LedgerRange:
    """Zeitraum aus Geschäftsjahr und/oder Von/Bis (explizite Daten haben Vorrang)."""
    fiscal_year = None
    if fiscal_year_id is not None:
        fiscal_year = session.get(FiscalYear, fiscal_year_id)
        if fiscal_year is None or fiscal_year.company_id != account.company_id:
            raise AccountLedgerError("Geschäftsjahr nicht gefunden.")
        date_from = date_from or fiscal_year.start_date
        date_to = date_to or fiscal_year.end_date
    if date_from and date_to and date_from > date_to:
        raise AccountLedgerError("date_from darf nicht nach date_to liegen.")
    return LedgerRange(date_from=date_from, date_to=date_to, fiscal_year=fiscal_year)


def _line_filter(account: Account):
    return (
        JournalEntryLine.account_id == account.id,
        JournalEntry.company_id == account.company_id,
        JournalEntry.source != SOURCE_CARRYFORWARD,
    )


def opening_balance(*, session: Session, account: Account, date_from: date | None) -> Decimal:
    """Saldo vor ``date_from``; Erfolgskonten nur ab Beginn dieses Geschäftsjahres."""
    if date_from is None:
        return ZERO
    stmt = (
        select(
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(*_line_filter(account), JournalEntry.entry_date < date_from)
    )
    if account.account_type not in BALANCE_SHEET_ACCOUNT_TYPES:
        fiscal_year = fiscal_year_for_date(session, account.company_id, date_from)
        if fiscal_year is not None:
            stmt = stmt.where(JournalEntry.entry_date >= fiscal_year.start_date)
    debit, credit = session.execute(stmt).one()
    return Decimal(debit) - Decimal(credit)


def _ordered_lines(account: Account, ledger_range: LedgerRange):
    stmt = (
        select(JournalEntryLine.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(*_line_filter(account))
    )
    if ledger_range.date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= ledger_range.date_from)
    if ledger_range.date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= ledger_range.date_to)
    return stmt


_ORDER = (JournalEntry.entry_date, JournalEntry.id, JournalEntryLine.line_number)


def _sums(session: Session, line_ids_stmt) -> tuple[int, Decimal, Decimal]:
    subquery = line_ids_stmt.subquery()
    count, debit, credit = session.execute(
        select(
            func.count(JournalEntryLine.id),
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        ).where(JournalEntryLine.id.in_(select(subquery.c.id)))
    ).one()
    return int(count), Decimal(debit), Decimal(credit)


def _contra_accounts(
    session: Session, account_id: int, entry_ids: list[int]
) -> dict[tuple[int, str], list[dict[str, Any]]]:
    """Gegenkonten je (Buchung, Seite): Konten der Gegenseite ohne das Konto selbst."""
    result: dict[tuple[int, str], list[dict[str, Any]]] = {}
    if not entry_ids:
        return result
    rows = session.execute(
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntryLine.debit_amount,
            Account.id,
            Account.code,
            Account.name,
        )
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(
            JournalEntryLine.journal_entry_id.in_(entry_ids),
            JournalEntryLine.account_id != account_id,
        )
        .order_by(JournalEntryLine.journal_entry_id, JournalEntryLine.line_number)
    ).all()
    for entry_id, debit_amount, contra_id, code, name in rows:
        # Gegenkonten einer Sollzeile stehen im Haben und umgekehrt.
        side = "credit" if debit_amount > 0 else "debit"
        contras = result.setdefault((entry_id, side), [])
        if all(contra["account_id"] != contra_id for contra in contras):
            contras.append({"account_id": contra_id, "code": code, "name": name})
    return result


def account_ledger(
    *,
    session: Session,
    account: Account,
    date_from: date | None = None,
    date_to: date | None = None,
    fiscal_year_id: int | None = None,
    limit: int = DEFAULT_LEDGER_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Kontoblatt eines Kontos, chronologisch, mit laufendem Saldo je Zeile.

    ``limit``/``offset`` blättern durch die Zeilen; der laufende Saldo berücksichtigt
    auch die Zeilen vor der angezeigten Seite.
    """
    ledger_range = resolve_ledger_range(
        session=session,
        account=account,
        date_from=date_from,
        date_to=date_to,
        fiscal_year_id=fiscal_year_id,
    )
    limit = max(1, min(int(limit), MAX_LEDGER_LIMIT))
    offset = max(0, int(offset))

    opening = opening_balance(session=session, account=account, date_from=ledger_range.date_from)
    line_ids = _ordered_lines(account, ledger_range)
    total, debit_total, credit_total = _sums(session, line_ids)
    _, debit_before, credit_before = (
        _sums(session, line_ids.order_by(*_ORDER).limit(offset)) if offset else (0, ZERO, ZERO)
    )

    cost_unit = aliased(ControllingUnit)
    profit_unit = aliased(ControllingUnit)
    rows = session.execute(
        select(
            JournalEntryLine,
            JournalEntry,
            TaxCode.code,
            cost_unit.code,
            profit_unit.code,
            BusinessPartner,
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .outerjoin(TaxCode, TaxCode.id == JournalEntryLine.tax_code_id)
        .outerjoin(cost_unit, cost_unit.id == JournalEntryLine.cost_center_id)
        .outerjoin(profit_unit, profit_unit.id == JournalEntryLine.profit_center_id)
        .outerjoin(BusinessPartner, BusinessPartner.id == JournalEntryLine.partner_id)
        .where(JournalEntryLine.id.in_(line_ids))
        .order_by(*_ORDER)
        .limit(limit)
        .offset(offset)
    ).all()
    contras = _contra_accounts(
        session, account.id, sorted({entry.id for _, entry, *_ in rows})
    )

    balance = opening + debit_before - credit_before
    lines: list[dict[str, Any]] = []
    for line, entry, tax_code, cost_center, profit_center, partner in rows:
        balance += line.debit_amount - line.credit_amount
        side = "debit" if line.debit_amount > 0 else "credit"
        lines.append(
            {
                "journal_entry_id": entry.id,
                "posting_number": entry.posting_number,
                "entry_date": entry.entry_date,
                "service_date": entry.service_date,
                "description": entry.description,
                "source": entry.source,
                "is_finalized": entry.is_finalized,
                "line_number": line.line_number,
                "line_description": line.description,
                "debit_amount": line.debit_amount,
                "credit_amount": line.credit_amount,
                "balance": balance,
                "tax_code": tax_code,
                "cost_center_code": cost_center,
                "profit_center_code": profit_center,
                "partner": (
                    {
                        "id": partner.id,
                        "name": partner.name,
                        "number": partner_display_number(partner, account.subledger) or None,
                    }
                    if partner is not None
                    else None
                ),
                "contra_accounts": contras.get((entry.id, side), []),
            }
        )

    fiscal_year = ledger_range.fiscal_year
    if fiscal_year is None and ledger_range.date_from is not None:
        candidate = fiscal_year_for_date(session, account.company_id, ledger_range.date_from)
        if candidate is not None and (
            ledger_range.date_to is None or ledger_range.date_to <= candidate.end_date
        ):
            fiscal_year = candidate
    return {
        "account": serialize_account(account),
        "period": {
            "date_from": ledger_range.date_from,
            "date_to": ledger_range.date_to,
            "fiscal_year_id": fiscal_year.id if fiscal_year else None,
            "fiscal_year": fiscal_year.label if fiscal_year else None,
        },
        "opening_balance": opening,
        "totals": {
            "debit_total": debit_total,
            "credit_total": credit_total,
            "closing_balance": opening + debit_total - credit_total,
        },
        "lines": lines,
        "total_lines": total,
        "limit": limit,
        "offset": offset,
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def serialize_account_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """Kontoblatt für API/MCP: Beträge als Dezimal-Strings, Daten als ISO-Format."""
    return _json_value(ledger)
