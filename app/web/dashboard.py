"""Dashboard-Startseite mit Kennzahlen und offenen Aufgaben."""

from __future__ import annotations

from flask import render_template

from app.services.dashboard import count_rows as _count
from app.services.dashboard import open_task_counts
from app.services.reports import balance_sheet_for_company, income_statement_for_company
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory
from app.web.journal import entry_overview
from domain.models import Account, Document, JournalEntry


@main_bp.get("/")
def index():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        stats = {"accounts": 0, "journal_entries": 0, "documents": 0}
        totals = None
        balance_totals = None
        recent_entries = []
        recent_overview: dict[int, dict] = {}
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
                    .limit(8)
                )
                .scalars()
                .all()
            )
            recent_overview = entry_overview(
                session, selected_company_id, [entry.id for entry in recent_entries]
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
        recent_overview=recent_overview,
        tasks=tasks,
    )
