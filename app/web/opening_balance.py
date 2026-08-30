"""Eröffnungsbilanz/Saldenübernahme aus einem Altsystem."""

from __future__ import annotations

from datetime import date

from flask import flash, redirect, render_template, request, url_for

from app.services.journal_entries import JournalEntryCreationError
from app.services.opening_balance import (
    OpeningBalanceError,
    book_opening_balance,
    find_carryforward_account,
    parse_balance_csv,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.services.journal_entry_validation import JournalEntryValidationError


@main_bp.get("/eroeffnungsbilanz")
def opening_balance_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        carryforward_account = None
        if selected_company_id:
            carryforward_account = find_carryforward_account(
                session=session, company_id=selected_company_id
            )

    return render_template(
        "eroeffnungsbilanz.html",
        companies=companies,
        selected_company_id=selected_company_id,
        carryforward_account=carryforward_account,
        today=date.today().isoformat(),
    )


@main_bp.post("/eroeffnungsbilanz")
def opening_balance_action():
    company_id = request.form.get("company_id", type=int)
    entry_date_raw = (request.form.get("entry_date") or "").strip()
    balances_raw = request.form.get("balances") or ""

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            entry_date = (
                date.fromisoformat(entry_date_raw) if entry_date_raw else date.today()
            )
        except ValueError:
            flash("Ungültiges Buchungsdatum.", "error")
            return redirect(url_for("main.opening_balance_page", company_id=company_id))

        try:
            balances = parse_balance_csv(balances_raw)
            entry = book_opening_balance(
                session=session,
                company_id=company_id,
                entry_date=entry_date,
                balances=balances,
                changed_by=changed_by(),
            )
        except (
            OpeningBalanceError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.opening_balance_page", company_id=company_id))

    flash(
        f"Saldenübernahme als Buchung {entry.posting_number} gebucht "
        f"({len(balances)} Kontensalden).",
        "success",
    )
    return redirect(url_for("main.journal_page", company_id=company_id))
