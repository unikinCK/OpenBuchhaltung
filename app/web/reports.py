"""Berichte: SuSa, GuV, Bilanz, CSV- und DATEV-Download."""

from __future__ import annotations

import csv
from datetime import date
from io import StringIO

from flask import flash, make_response, redirect, render_template, request, url_for

from app.services.account_chart_check import detect_company_chart
from app.services.datev_export import (
    DatevExportError,
    DatevFiscalYear,
    datev_fiscal_years,
    resolve_datev_export_period,
)
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


def _datev_selection(
    fiscal_years: list[DatevFiscalYear], date_from: date | None, date_to: date | None
) -> dict[str, object]:
    """Vorauswahl des DATEV-Exports auf der Berichte-Seite.

    Die zuletzt abgeschickte Auswahl (nach einem Fehler), sonst das
    Wirtschaftsjahr des Auswertungszeitraums, sonst das jüngste mit Buchungen.
    """
    by_id = {fiscal_year.id: fiscal_year for fiscal_year in fiscal_years}
    selected = by_id.get(request.args.get("datev_fiscal_year_id", type=int))
    anchor = date_to or date_from
    if selected is None and anchor:
        selected = next(
            (year for year in fiscal_years if year.start_date <= anchor <= year.end_date), None
        )
    if selected is None:
        selected = next((year for year in fiscal_years if year.entry_count), None)
    if selected is None and fiscal_years:
        selected = fiscal_years[0]
    return {
        "fiscal_year_id": selected.id if selected else None,
        "date_from": (request.args.get("datev_date_from") or "").strip(),
        "date_to": (request.args.get("datev_date_to") or "").strip(),
    }


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
        datev_chart = None
        datev_years: list[DatevFiscalYear] = []
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
            # Kontenrahmen, nach dem der DATEV-Export Automatikkonten setzt.
            datev_chart = detect_company_chart(session=session, company_id=selected_company_id)
            datev_years = datev_fiscal_years(session=session, company_id=selected_company_id)

    return render_template(
        "berichte.html",
        companies=companies,
        selected_company_id=selected_company_id,
        trial_balance=trial_balance,
        income_statement=income_statement,
        balance_sheet=balance_sheet,
        datev_chart=datev_chart,
        datev_fiscal_years=datev_years,
        datev_selection=_datev_selection(datev_years, date_from, date_to),
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


@main_bp.get("/reports/datev.csv")
def download_datev_csv():
    """DATEV-Export der Berichte-Seite: Auswahl prüfen, dann Download über die API.

    Fehler (Zeitraum außerhalb des Wirtschaftsjahres) erscheinen als Hinweis auf
    der Berichte-Seite statt als JSON-Antwort der API.
    """
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        flash("Gesellschaft für Export fehlt.", "error")
        return redirect(url_for("main.reports_page"))
    fiscal_year_id = request.args.get("fiscal_year_id", type=int)
    _, date_from, date_to = search_args()

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            period = resolve_datev_export_period(
                session=session,
                company_id=company_id,
                fiscal_year_id=fiscal_year_id,
                date_from=date_from,
                date_to=date_to,
            )
        except DatevExportError as exc:
            flash(f"DATEV-Export: {exc}", "error")
            return redirect(
                url_for(
                    "main.reports_page",
                    company_id=company_id,
                    **filter_url_args(
                        None,
                        None,
                        None,
                        datev_fiscal_year_id=fiscal_year_id,
                        datev_date_from=date_from.isoformat() if date_from else None,
                        datev_date_to=date_to.isoformat() if date_to else None,
                    ),
                )
            )

    return redirect(
        url_for(
            "api.export_datev_csv",
            company_id=company_id,
            **filter_url_args(None, date_from, date_to, fiscal_year_id=period.fiscal_year_id),
        )
    )
