"""Dashboard-Startseite mit Kennzahlen und offenen Aufgaben."""

from __future__ import annotations

from datetime import date

from flask import render_template
from sqlalchemy import func, select

from app.services.journal_templates import due_templates
from app.services.reports import balance_sheet_for_company, income_statement_for_company
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory
from domain.models import (
    Account,
    BankTransaction,
    Document,
    JournalEntry,
    OpenItem,
    ReceiptMatchSuggestion,
)


def _count(session, stmt) -> int:
    return session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def open_task_counts(session, company_id: int) -> dict[str, int]:
    """Offene Aufgaben einer Gesellschaft für die Dashboard-Kacheln."""
    return {
        "open_bank_transactions": _count(
            session,
            scoped_select(BankTransaction, company_id=company_id).where(
                BankTransaction.status == "open"
            ),
        ),
        "unlinked_documents": _count(
            session,
            scoped_select(Document, company_id=company_id).where(
                Document.journal_entry_id.is_(None)
            ),
        ),
        "overdue_open_items": _count(
            session,
            scoped_select(OpenItem, company_id=company_id).where(
                OpenItem.status == "open",
                OpenItem.due_date.is_not(None),
                OpenItem.due_date < date.today(),
            ),
        ),
        "pending_match_suggestions": _count(
            session,
            scoped_select(ReceiptMatchSuggestion, company_id=company_id).where(
                ReceiptMatchSuggestion.status == "offen"
            ),
        ),
        "due_templates": len(due_templates(session=session, company_id=company_id)),
    }


@main_bp.get("/")
def index():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        stats = {"accounts": 0, "journal_entries": 0, "documents": 0}
        totals = None
        balance_totals = None
        recent_entries = []
        tasks = None
        if selected_company_id:
            stats["accounts"] = _count(
                session, scoped_select(Account, company_id=selected_company_id)
            )
            stats["documents"] = _count(
                session, scoped_select(Document, company_id=selected_company_id)
            )
            stats["journal_entries"] = _count(
                session, scoped_select(JournalEntry, company_id=selected_company_id)
            )
            recent_entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id)
                    .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
                    .limit(5)
                )
                .scalars()
                .all()
            )
            tasks = open_task_counts(session, selected_company_id)
            totals = income_statement_for_company(
                session=session, company_id=selected_company_id
            )["totals"]
            balance_totals = balance_sheet_for_company(
                session=session, company_id=selected_company_id
            )["totals"]

    return render_template(
        "dashboard.html",
        companies=companies,
        selected_company_id=selected_company_id,
        stats=stats,
        totals=totals,
        balance_totals=balance_totals,
        recent_entries=recent_entries,
        tasks=tasks,
    )
