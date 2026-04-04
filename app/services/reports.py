from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models import Account, JournalEntry, JournalEntryLine


def trial_balance_for_company(*, session: Session, company_id: int) -> list[dict[str, Decimal | str]]:
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
