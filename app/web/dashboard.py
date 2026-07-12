"""Dashboard-Startseite mit Kennzahlen."""

from __future__ import annotations

from flask import render_template

from app.services.reports import balance_sheet_for_company, income_statement_for_company
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory
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
        if selected_company_id:
            stats["accounts"] = len(
                session.execute(scoped_select(Account, company_id=selected_company_id))
                .scalars()
                .all()
            )
            stats["documents"] = len(
                session.execute(scoped_select(Document, company_id=selected_company_id))
                .scalars()
                .all()
            )
            entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                )
                .scalars()
                .all()
            )
            stats["journal_entries"] = len(entries)
            recent_entries = entries[:5]
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
    )
