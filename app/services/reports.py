from __future__ import annotations

import csv
from io import StringIO
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models import Account, JournalEntry, JournalEntryLine


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
        debit = debit_total or Decimal("0.00")
        credit = credit_total or Decimal("0.00")
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


def _decimal_to_str(value: Decimal) -> str:
    return f"{value:.2f}"


def trial_balance_csv_for_company(*, session: Session, company_id: int) -> str:
    rows = trial_balance_for_company(session=session, company_id=company_id)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["code", "name", "debit_total", "credit_total", "balance"])
    for row in rows:
        writer.writerow(
            [
                row["code"],
                row["name"],
                _decimal_to_str(row["debit_total"]),
                _decimal_to_str(row["credit_total"]),
                _decimal_to_str(row["balance"]),
            ]
        )

    return output.getvalue()


def journal_entries_csv_for_company(*, session: Session, company_id: int) -> str:
    rows = session.execute(
        select(
            JournalEntry.posting_number,
            JournalEntry.entry_date,
            JournalEntry.description,
            JournalEntryLine.line_number,
            Account.code,
            Account.name,
            JournalEntryLine.description,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.currency_code,
        )
        .join(JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(JournalEntry.company_id == company_id)
        .order_by(JournalEntry.entry_date, JournalEntry.posting_number, JournalEntryLine.line_number)
    ).all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "posting_number",
            "entry_date",
            "entry_description",
            "line_number",
            "account_code",
            "account_name",
            "line_description",
            "debit_amount",
            "credit_amount",
            "currency_code",
        ]
    )
    for (
        posting_number,
        entry_date,
        entry_description,
        line_number,
        account_code,
        account_name,
        line_description,
        debit_amount,
        credit_amount,
        currency_code,
    ) in rows:
        writer.writerow(
            [
                posting_number,
                entry_date.isoformat(),
                entry_description,
                line_number,
                account_code,
                account_name,
                line_description or "",
                _decimal_to_str(debit_amount or Decimal("0.00")),
                _decimal_to_str(credit_amount or Decimal("0.00")),
                currency_code,
            ]
        )

    return output.getvalue()
