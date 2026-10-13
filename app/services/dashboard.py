"""Dashboard-Kennzahlen: offene Aufgaben, Bankstände und Ergebnis einer Gesellschaft.

Gemeinsame Grundlage für die Dashboard-Seite, ``GET /api/v1/dashboard`` und das
MCP-Tool ``get_dashboard`` (u. a. Startbildschirm und Widgets der iOS-App).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.bank_import import bank_reconciliation
from app.services.journal_templates import due_templates
from app.services.reports import (
    balance_sheet_for_company,
    fiscal_year_for_date,
    income_statement_for_company,
)
from app.services.scoping import scoped_select
from domain.models import BankTransaction, Document, OpenItem, ReceiptMatchSuggestion


def count_rows(session: Session, stmt) -> int:
    return session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def open_task_counts(session: Session, company_id: int) -> dict[str, int]:
    """Offene Aufgaben einer Gesellschaft (Dashboard-Kacheln, App-Inbox)."""
    return {
        "open_bank_transactions": count_rows(
            session,
            scoped_select(BankTransaction, company_id=company_id).where(
                BankTransaction.status == "open"
            ),
        ),
        "unlinked_documents": count_rows(
            session,
            scoped_select(Document, company_id=company_id).where(
                Document.journal_entry_id.is_(None)
            ),
        ),
        "overdue_open_items": count_rows(
            session,
            scoped_select(OpenItem, company_id=company_id).where(
                OpenItem.status == "open",
                OpenItem.due_date.is_not(None),
                OpenItem.due_date < date.today(),
            ),
        ),
        "pending_match_suggestions": count_rows(
            session,
            scoped_select(ReceiptMatchSuggestion, company_id=company_id).where(
                ReceiptMatchSuggestion.status == "offen"
            ),
        ),
        "due_templates": len(due_templates(session=session, company_id=company_id)),
    }


def dashboard_summary(
    *, session: Session, company_id: int, as_of: date | None = None
) -> dict[str, object]:
    """Kompakte Übersicht für mobile Clients.

    Das Ergebnis läuft vom Beginn des Geschäftsjahres, das ``as_of`` enthält, bis
    ``as_of`` (ohne Geschäftsjahr: alle Buchungen bis ``as_of``). Bankstände sind
    die Buchsalden der Konten mit importierten Umsätzen, daneben die Summe der
    Kontoauszugszeilen.
    """
    as_of = as_of or date.today()
    fiscal_year = fiscal_year_for_date(session, company_id, as_of)
    income = income_statement_for_company(
        session=session,
        company_id=company_id,
        date_from=fiscal_year.start_date if fiscal_year else None,
        date_to=as_of,
    )["totals"]
    balance = balance_sheet_for_company(session=session, company_id=company_id, date_to=as_of)[
        "totals"
    ]
    bank_rows = bank_reconciliation(session=session, company_id=company_id)
    open_transactions = dict(
        session.execute(
            select(BankTransaction.bank_account_id, func.count())
            .where(BankTransaction.company_id == company_id, BankTransaction.status == "open")
            .group_by(BankTransaction.bank_account_id)
        ).all()
    )
    latest_booking = dict(
        session.execute(
            select(BankTransaction.bank_account_id, func.max(BankTransaction.booking_date))
            .where(BankTransaction.company_id == company_id)
            .group_by(BankTransaction.bank_account_id)
        ).all()
    )
    return {
        "as_of": as_of,
        "fiscal_year": fiscal_year.label if fiscal_year else None,
        "tasks": open_task_counts(session, company_id),
        "bank_accounts": [
            {
                "account_id": row.account_id,
                "account_code": row.account_code,
                "account_name": row.account_name,
                "book_balance": row.book_balance,
                "statement_total": row.statement_total,
                "open_transactions": open_transactions.get(row.account_id, 0),
                "latest_booking_date": latest_booking.get(row.account_id),
            }
            for row in bank_rows
        ],
        "income_statement": income,
        "balance_sheet": {
            "total_assets": balance["total_assets"],
            "total_liabilities_and_equity": balance["total_liabilities_and_equity"],
        },
    }
