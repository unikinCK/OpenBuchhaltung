from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    CARRYFORWARD_SOURCES,
    SOURCE_CARRYFORWARD,
    SOURCE_YEAR_END_CLOSE,
)
from domain.models import Account, FiscalYear, JournalEntry, JournalEntryLine, Period

ZERO = Decimal("0.00")

# SKR-Importe verwenden "income", manuell angelegte Konten häufig "revenue".
REVENUE_ACCOUNT_TYPES = {"revenue", "income"}
BALANCE_SHEET_ACCOUNT_TYPES = {"asset", "liability", "equity"}

# Abschlussbuchungen (Periode 13: AfA, Rückstellungen, Ergebnisvortrag …) werden
# standardmäßig ausgeklammert — Auswertungen zeigen dann den Stand vor dem
# Jahresabschluss (BWA-Sicht). Mit ``include_closing_entries=True`` fließen sie
# ein; der Ergebnisvortrag selbst bleibt in GuV und Ertragsteuern immer außen
# vor, weil er nur die Erfolgskonten glattstellt und kein Ergebnis darstellt.


def _apply_date_filter(stmt, *, date_from: date | None, date_to: date | None):
    """Beschränkt die Auswertung auf Buchungen mit ``entry_date`` im Zeitraum."""
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    return stmt


def _period(
    date_from: date | None, date_to: date | None, include_closing_entries: bool
) -> dict[str, str | bool | None]:
    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "include_closing_entries": include_closing_entries,
    }


def _balance_rows(
    session: Session,
    company_id: int,
    *,
    date_from: date | None,
    date_to: date | None,
    conditions: list,
) -> list[dict[str, Decimal | str]]:
    """Soll-/Haben-Summen je Konto für Buchungen der Gesellschaft."""
    stmt = (
        select(
            Account.code,
            Account.name,
            Account.account_type,
            func.sum(JournalEntryLine.debit_amount).label("debit_total"),
            func.sum(JournalEntryLine.credit_amount).label("credit_total"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Period, Period.id == JournalEntry.period_id)
        .where(JournalEntry.company_id == company_id)
        .group_by(Account.id, Account.code, Account.name, Account.account_type)
        .order_by(Account.code)
    )
    stmt = _apply_date_filter(stmt, date_from=date_from, date_to=date_to)
    for condition in conditions:
        stmt = stmt.where(condition)
    return [
        {
            "code": code,
            "name": name,
            "account_type": account_type,
            "debit_total": debit_total or ZERO,
            "credit_total": credit_total or ZERO,
        }
        for code, name, account_type, debit_total, credit_total in session.execute(stmt).all()
    ]


def fiscal_year_for_date(session: Session, company_id: int, dt: date) -> FiscalYear | None:
    """Das Geschäftsjahr der Gesellschaft, das ``dt`` enthält (falls vorhanden)."""
    return (
        session.execute(
            select(FiscalYear)
            .where(
                FiscalYear.company_id == company_id,
                FiscalYear.start_date <= dt,
                FiscalYear.end_date >= dt,
            )
            .order_by(FiscalYear.start_date.desc())
        )
        .scalars()
        .first()
    )


def trial_balance_for_company(
    *,
    session: Session,
    company_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    include_closing_entries: bool = False,
) -> list[dict[str, Decimal | str]]:
    """Summen- und Saldenliste.

    Saldovorträge (EB-Werte) zählen nur bei einer Auswertung ab einem
    Startdatum mit — ohne ``date_from`` enthalten die kumulierten Summen die
    Vorjahre bereits, ein zusätzlicher Vortrag würde sie doppeln.
    """
    conditions = []
    if not include_closing_entries:
        conditions.append(Period.is_closing.is_(False))
    if date_from is None:
        conditions.append(JournalEntry.source != SOURCE_CARRYFORWARD)
    rows = _balance_rows(
        session, company_id, date_from=date_from, date_to=date_to, conditions=conditions
    )
    return [
        {
            "code": row["code"],
            "name": row["name"],
            "debit_total": row["debit_total"],
            "credit_total": row["credit_total"],
            "balance": row["debit_total"] - row["credit_total"],
        }
        for row in rows
    ]


def account_balances_by_type(
    *,
    session: Session,
    company_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    include_closing_entries: bool = False,
) -> list[dict[str, Decimal | str]]:
    """Kontensalden je Kontoart für Erfolgsrechnungen (ohne Vortragsbuchungen)."""
    conditions = [JournalEntry.source.notin_(CARRYFORWARD_SOURCES)]
    if not include_closing_entries:
        conditions.append(Period.is_closing.is_(False))
    return _balance_rows(
        session, company_id, date_from=date_from, date_to=date_to, conditions=conditions
    )


def _profit_and_loss(
    rows: list[dict[str, Decimal | str]],
) -> tuple[list[dict[str, Decimal | str]], list[dict[str, Decimal | str]]]:
    revenues: list[dict[str, Decimal | str]] = []
    expenses: list[dict[str, Decimal | str]] = []
    for row in rows:
        if row["account_type"] in REVENUE_ACCOUNT_TYPES:
            amount = row["credit_total"] - row["debit_total"]
            revenues.append({"code": row["code"], "name": row["name"], "amount": amount})
        elif row["account_type"] == "expense":
            amount = row["debit_total"] - row["credit_total"]
            expenses.append({"code": row["code"], "name": row["name"], "amount": amount})
    return revenues, expenses


def income_statement_for_company(
    *,
    session: Session,
    company_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    include_closing_entries: bool = False,
) -> dict[str, object]:
    """Gewinn- und Verlustrechnung.

    Der Ergebnisvortrag des Jahresabschlusses wird nie mitgerechnet — er
    stellt die Erfolgskonten nur glatt; die GuV eines abgeschlossenen Jahres
    bleibt damit aussagekräftig.
    """
    balances = account_balances_by_type(
        session=session,
        company_id=company_id,
        date_from=date_from,
        date_to=date_to,
        include_closing_entries=include_closing_entries,
    )
    revenues, expenses = _profit_and_loss(balances)
    total_revenue = sum((row["amount"] for row in revenues), ZERO)
    total_expense = sum((row["amount"] for row in expenses), ZERO)
    net_income = total_revenue - total_expense

    return {
        "period": _period(date_from, date_to, include_closing_entries),
        "revenues": revenues,
        "expenses": expenses,
        "totals": {
            "total_revenue": total_revenue,
            "total_expense": total_expense,
            "net_income": net_income,
        },
    }


def balance_sheet_for_company(
    *,
    session: Session,
    company_id: int,
    date_to: date | None = None,
    include_closing_entries: bool = False,
) -> dict[str, object]:
    """Bilanz zum Stichtag ``date_to`` (ohne Angabe: heute).

    Kumuliert alle Buchungen bis zum Stichtag. Saldovorträge zählen nicht mit
    (die Vorjahre sind bereits enthalten). Für das Geschäftsjahr des
    Stichtags gilt der Schalter für Abschlussbuchungen; sein Ergebnisvortrag
    bleibt immer außen vor, sodass das Jahresergebnis als eigene Position
    erscheint. Vorjahre fließen vollständig ein (ihr Ergebnis steht im
    Gewinnvortrag). Aktiva, Passiva und Jahresergebnis stammen aus derselben
    Buchungsmenge — die Bilanz geht daher immer auf.
    """
    as_of = date_to or date.today()
    fiscal_year = fiscal_year_for_date(session, company_id, as_of)
    conditions = [JournalEntry.source != SOURCE_CARRYFORWARD]
    if fiscal_year is not None:
        in_scope = JournalEntry.fiscal_year_id == fiscal_year.id
        conditions.append(~and_(in_scope, JournalEntry.source == SOURCE_YEAR_END_CLOSE))
        if not include_closing_entries:
            conditions.append(~and_(in_scope, Period.is_closing.is_(True)))
    balances = _balance_rows(
        session, company_id, date_from=None, date_to=date_to, conditions=conditions
    )

    assets: list[dict[str, Decimal | str]] = []
    liabilities_and_equity: list[dict[str, Decimal | str]] = []
    for row in balances:
        account_type = row["account_type"]
        if account_type == "asset":
            amount = row["debit_total"] - row["credit_total"]
            assets.append({"code": row["code"], "name": row["name"], "amount": amount})
        elif account_type in {"liability", "equity"}:
            amount = row["credit_total"] - row["debit_total"]
            liabilities_and_equity.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "amount": amount,
                    "account_type": account_type,
                }
            )

    revenues, expenses = _profit_and_loss(balances)
    net_income = sum((row["amount"] for row in revenues), ZERO) - sum(
        (row["amount"] for row in expenses), ZERO
    )
    if net_income != ZERO:
        liabilities_and_equity.append(
            {
                "code": "P&L",
                "name": "Jahresergebnis",
                "amount": net_income,
                "account_type": "equity",
            }
        )

    total_assets = sum((row["amount"] for row in assets), ZERO)
    total_liabilities_equity = sum((row["amount"] for row in liabilities_and_equity), ZERO)

    return {
        "period": {
            "as_of": date_to.isoformat() if date_to else None,
            "fiscal_year": fiscal_year.label if fiscal_year else None,
            "include_closing_entries": include_closing_entries,
        },
        "assets": assets,
        "liabilities_and_equity": liabilities_and_equity,
        "totals": {
            "total_assets": total_assets,
            "total_liabilities_and_equity": total_liabilities_equity,
            "difference": total_assets - total_liabilities_equity,
            "is_balanced": total_assets == total_liabilities_equity,
        },
    }
