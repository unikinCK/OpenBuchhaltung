"""Berichte: SuSa, GuV, Bilanz und CSV-Download."""

from __future__ import annotations

import csv
from datetime import date
from io import StringIO

from flask import flash, make_response, redirect, render_template, request, url_for

from app.services.reports import (
    balance_sheet_for_company,
    income_statement_for_company,
    trial_balance_for_company,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    company_context,
    filter_url_args,
    get_session_factory,
    require_company_access,
    search_args,
)


def _include_closing_entries() -> bool:
    """Schalter „inkl. Abschlussbuchungen“ (Checkbox bzw. Query-Parameter)."""
    raw = (request.args.get("include_closing_entries") or "").strip().lower()
    return raw in {"1", "true", "on", "yes", "ja"}


@main_bp.get("/berichte")
def reports_page():
    _, date_from, date_to = search_args()
    include_closing_entries = _include_closing_entries()
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        trial_balance = []
        income_statement = {"revenues": [], "expenses": [], "totals": {}}
        balance_sheet = {"assets": [], "liabilities_and_equity": [], "totals": {}, "period": {}}
        if selected_company_id:
            trial_balance = trial_balance_for_company(
                session=session,
                company_id=selected_company_id,
                date_from=date_from,
                date_to=date_to,
                include_closing_entries=include_closing_entries,
            )
            income_statement = income_statement_for_company(
                session=session,
                company_id=selected_company_id,
                date_from=date_from,
                date_to=date_to,
                include_closing_entries=include_closing_entries,
            )
            balance_sheet = balance_sheet_for_company(
                session=session,
                company_id=selected_company_id,
                date_to=date_to,
                include_closing_entries=include_closing_entries,
            )

    return render_template(
        "berichte.html",
        companies=companies,
        selected_company_id=selected_company_id,
        trial_balance=trial_balance,
        income_statement=income_statement,
        balance_sheet=balance_sheet,
        date_from=date_from,
        date_to=date_to,
        include_closing_entries=include_closing_entries,
        report_args=filter_url_args(
            None,
            date_from,
            date_to,
            include_closing_entries="1" if include_closing_entries else None,
        ),
    )


@main_bp.get("/reports/trial-balance.csv")
def download_trial_balance_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        flash("Gesellschaft für Export fehlt.", "error")
        return redirect(url_for("main.reports_page"))
    _, date_from, date_to = search_args()
    include_closing_entries = _include_closing_entries()

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        rows = trial_balance_for_company(
            session=session,
            company_id=company_id,
            date_from=date_from,
            date_to=date_to,
            include_closing_entries=include_closing_entries,
        )

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Konto", "Name", "Soll", "Haben", "Saldo"])
    for row in rows:
        writer.writerow(
            [
                row["code"],
                row["name"],
                f"{row['debit_total']:.2f}",
                f"{row['credit_total']:.2f}",
                f"{row['balance']:.2f}",
            ]
        )

    csv_content = output.getvalue()
    response = make_response(csv_content)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=susa-{company_id}-{date.today().isoformat()}.csv"
    )
    return response
