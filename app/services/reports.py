from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models import Account, JournalEntry, JournalEntryLine

ZERO = Decimal("0.00")


def trial_balance_for_company(
    *,
    session: Session,
    company_id: int,
) -> list[dict[str, Decimal | str]]:
    rows = session.execute(
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
    *, session: Session, company_id: int
) -> list[dict[str, Decimal | str]]:
    rows = session.execute(
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


def income_statement_for_company(*, session: Session, company_id: int) -> dict[str, object]:
    balances = account_balances_by_type(session=session, company_id=company_id)
    revenues: list[dict[str, Decimal | str]] = []
    expenses: list[dict[str, Decimal | str]] = []

    for row in balances:
        if row["account_type"] == "revenue":
            amount = row["credit_total"] - row["debit_total"]
            revenues.append({"code": row["code"], "name": row["name"], "amount": amount})
        elif row["account_type"] == "expense":
            amount = row["debit_total"] - row["credit_total"]
            expenses.append({"code": row["code"], "name": row["name"], "amount": amount})

    total_revenue = sum((row["amount"] for row in revenues), ZERO)
    total_expense = sum((row["amount"] for row in expenses), ZERO)
    net_income = total_revenue - total_expense

    return {
        "revenues": revenues,
        "expenses": expenses,
        "totals": {
            "total_revenue": total_revenue,
            "total_expense": total_expense,
            "net_income": net_income,
        },
    }


def balance_sheet_for_company(*, session: Session, company_id: int) -> dict[str, object]:
    balances = account_balances_by_type(session=session, company_id=company_id)
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

    income_statement = income_statement_for_company(session=session, company_id=company_id)
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
        "assets": assets,
        "liabilities_and_equity": liabilities_and_equity,
        "totals": {
            "total_assets": total_assets,
            "total_liabilities_and_equity": total_liabilities_equity,
            "difference": total_assets - total_liabilities_equity,
            "is_balanced": total_assets == total_liabilities_equity,
        },
    }
