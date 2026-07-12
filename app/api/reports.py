"""Berichte: Summen-/Saldenliste, GuV und Bilanz."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_scoped_company,
    date_arg,
    get_session_factory,
)
from app.services.reports import (
    balance_sheet_for_company,
    income_statement_for_company,
    trial_balance_for_company,
)


@api_bp.get("/trial-balance")
def get_trial_balance():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        rows = trial_balance_for_company(
            session=session, company_id=company_id, date_from=date_from, date_to=date_to
        )

    return (
        jsonify(
            {
                "company_id": company_id,
                "period": {
                    "date_from": date_from.isoformat() if date_from else None,
                    "date_to": date_to.isoformat() if date_to else None,
                },
                "rows": [
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "debit_total": str(row["debit_total"]),
                        "credit_total": str(row["credit_total"]),
                        "balance": str(row["balance"]),
                    }
                    for row in rows
                ],
            }
        ),
        200,
    )


@api_bp.get("/income-statement")
def get_income_statement():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        report = income_statement_for_company(
            session=session, company_id=company_id, date_from=date_from, date_to=date_to
        )

    return (
        jsonify(
            {
                "company_id": company_id,
                "period": report["period"],
                "revenues": [
                    {"code": row["code"], "name": row["name"], "amount": str(row["amount"])}
                    for row in report["revenues"]
                ],
                "expenses": [
                    {"code": row["code"], "name": row["name"], "amount": str(row["amount"])}
                    for row in report["expenses"]
                ],
                "totals": {key: str(value) for key, value in report["totals"].items()},
            }
        ),
        200,
    )


@api_bp.get("/balance-sheet")
def get_balance_sheet():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    # Bilanz ist eine Stichtagsbetrachtung: date_to (bzw. Alias as_of) = Stichtag.
    try:
        date_to = date_arg("date_to") or date_arg("as_of")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        report = balance_sheet_for_company(
            session=session, company_id=company_id, date_to=date_to
        )

    totals = report["totals"]
    return (
        jsonify(
            {
                "company_id": company_id,
                "period": report["period"],
                "assets": [
                    {"code": row["code"], "name": row["name"], "amount": str(row["amount"])}
                    for row in report["assets"]
                ],
                "liabilities_and_equity": [
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "account_type": row["account_type"],
                        "amount": str(row["amount"]),
                    }
                    for row in report["liabilities_and_equity"]
                ],
                "totals": {
                    "total_assets": str(totals["total_assets"]),
                    "total_liabilities_and_equity": str(totals["total_liabilities_and_equity"]),
                    "difference": str(totals["difference"]),
                    "is_balanced": totals["is_balanced"],
                },
            }
        ),
        200,
    )
