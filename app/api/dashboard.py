"""Dashboard: offene Aufgaben, Bankstände und Ergebnis (Startbildschirm mobiler Clients)."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import DateArgError, api_scoped_company, date_arg, get_session_factory
from app.services.dashboard import dashboard_summary


@api_bp.get("/dashboard")
def get_dashboard():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    try:
        as_of = date_arg("as_of")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        summary = dashboard_summary(session=session, company_id=company_id, as_of=as_of)

    income = summary["income_statement"]
    balance = summary["balance_sheet"]
    return (
        jsonify(
            {
                "company_id": company_id,
                "as_of": summary["as_of"].isoformat(),
                "fiscal_year": summary["fiscal_year"],
                "tasks": summary["tasks"],
                "bank_accounts": [
                    {
                        **row,
                        "book_balance": str(row["book_balance"]),
                        "statement_total": str(row["statement_total"]),
                        "latest_booking_date": (
                            row["latest_booking_date"].isoformat()
                            if row["latest_booking_date"]
                            else None
                        ),
                    }
                    for row in summary["bank_accounts"]
                ],
                "income_statement": {key: str(value) for key, value in income.items()},
                "balance_sheet": {key: str(value) for key, value in balance.items()},
            }
        ),
        200,
    )
