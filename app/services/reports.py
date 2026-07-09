from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models import Account, JournalEntry, JournalEntryLine

ZERO = Decimal("0.00")

# SKR-Importe verwenden "income", manuell angelegte Konten häufig "revenue".
REVENUE_ACCOUNT_TYPES = {"revenue", "income"}


def _apply_date_filter(stmt, *, date_from: date | None, date_to: date | None):
    """Beschränkt die Auswertung auf Buchungen mit ``entry_date`` im Zeitraum."""
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    return stmt


def _period(date_from: date | None, date_to: date | None) -> dict[str, str | None]:
    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }


def trial_balance_for_company(
    *,
    session: Session,
    company_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Decimal | str]]:
    stmt = (
        select(
            Account.code,
            Account.name,
            func.sum(JournalEntryLine.debit_amount).label("debit_total"),
            func.sum(JournalEntryLine.credit_amount).label("credit_total"),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(JournalEntry.company_id == company_id)
        .group_by(Account.id, Account.code, Account.name)
        .order_by(Account.code)
    )
    rows = session.execute(
        _apply_date_filter(stmt, date_from=date_from, date_to=date_to)
    ).all()

    report: list[dict[str, Decimal | str]] = []
    for code, name, debit_total, credit_total in rows:
        debit = debit_total or ZERO
        credit = credit_total or ZERO
        report.append(
            {
                "code": code,
                "name": name,
                "debit_total": debit,
                "credit_total": credit,
                "balance": debit - credit,
            }
        )

    return report


def account_balances_by_type(
    *,
    session: Session,
    company_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Decimal | str]]:
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
        .where(JournalEntry.company_id == company_id)
        .group_by(Account.id, Account.code, Account.name, Account.account_type)
        .order_by(Account.code)
    )
    rows = session.execute(
        _apply_date_filter(stmt, date_from=date_from, date_to=date_to)
    ).all()

    return [
        {
            "code": code,
            "name": name,
            "account_type": account_type,
            "debit_total": debit_total or ZERO,
            "credit_total": credit_total or ZERO,
        }
        for code, name, account_type, debit_total, credit_total in rows
    ]


def income_statement_for_company(
    *,
    session: Session,
    company_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, object]:
    balances = account_balances_by_type(
        session=session, company_id=company_id, date_from=date_from, date_to=date_to
    )
    revenues: list[dict[str, Decimal | str]] = []
    expenses: list[dict[str, Decimal | str]] = []

    for row in balances:
        if row["account_type"] in REVENUE_ACCOUNT_TYPES:
            amount = row["credit_total"] - row["debit_total"]
            revenues.append({"code": row["code"], "name": row["name"], "amount": amount})
        elif row["account_type"] == "expense":
            amount = row["debit_total"] - row["credit_total"]
            expenses.append({"code": row["code"], "name": row["name"], "amount": amount})

    total_revenue = sum((row["amount"] for row in revenues), ZERO)
    total_expense = sum((row["amount"] for row in expenses), ZERO)
    net_income = total_revenue - total_expense

    return {
        "period": _period(date_from, date_to),
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
) -> dict[str, object]:
    # Die Bilanz ist eine Stichtagsbetrachtung: alle Buchungen bis einschließlich
    # ``date_to`` (Stichtag). Ohne ``date_to`` werden alle Buchungen berücksichtigt.
    balances = account_balances_by_type(
        session=session, company_id=company_id, date_to=date_to
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

    income_statement = income_statement_for_company(
        session=session, company_id=company_id, date_to=date_to
    )
    net_income = income_statement["totals"]["net_income"]
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
        "period": {"as_of": date_to.isoformat() if date_to else None},
        "assets": assets,
        "liabilities_and_equity": liabilities_and_equity,
        "totals": {
            "total_assets": total_assets,
            "total_liabilities_and_equity": total_liabilities_equity,
            "difference": total_assets - total_liabilities_equity,
            "is_balanced": total_assets == total_liabilities_equity,
        },
    }
