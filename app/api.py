from __future__ import annotations

import csv
from datetime import date
from io import StringIO

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from app.services.account_hierarchy import resolve_parent_account_id
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.mcp_client import MCPError, call_mcp_server
from app.services.reports import (
    balance_sheet_for_company,
    income_statement_for_company,
    trial_balance_for_company,
)
from app.services.scoping import scoped_select
from domain.models import Account, Company, JournalEntry, JournalEntryLine, Tenant
from domain.services.journal_entry_validation import JournalEntryValidationError

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _validation_error(message: str, *, details: list[dict[str, str]] | None = None):
    payload: dict[str, object] = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), 422


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


@api_bp.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@api_bp.post("/tenants")
def create_tenant_with_company():
    payload = request.get_json(silent=True) or {}
    tenant_name = (payload.get("tenant_name") or "").strip()
    company_name = (payload.get("company_name") or "").strip()
    currency_code = (payload.get("currency_code") or "EUR").strip()

    if not tenant_name or not company_name:
        return jsonify({"error": "tenant_name and company_name are required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        tenant = Tenant(name=tenant_name)
        company = Company(name=company_name, currency_code=currency_code, tenant=tenant)
        session.add_all([tenant, company])

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Tenant or company already exists."}), 409

        return (
            jsonify(
                {
                    "tenant": {"id": tenant.id, "name": tenant.name},
                    "company": {
                        "id": company.id,
                        "name": company.name,
                        "tenant_id": company.tenant_id,
                        "currency_code": company.currency_code,
                    },
                }
            ),
            201,
        )


@api_bp.get("/companies")
def list_companies():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies = session.execute(scoped_select(Company).order_by(Company.name)).scalars().all()

    return (
        jsonify(
            [
                {
                    "id": company.id,
                    "tenant_id": company.tenant_id,
                    "name": company.name,
                    "currency_code": company.currency_code,
                }
                for company in companies
            ]
        ),
        200,
    )


@api_bp.post("/accounts")
def create_account():
    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    account_type = (payload.get("account_type") or "").strip()

    if not company_id or not code or not name or not account_type:
        return jsonify({"error": "company_id, code, name and account_type are required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
            parent_account_id=resolve_parent_account_id(
                session=session, company_id=company.id, code=code
            ),
        )
        session.add(account)

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Account code already exists for this company."}), 409

        return (
            jsonify(
                {
                    "id": account.id,
                    "tenant_id": account.tenant_id,
                    "company_id": account.company_id,
                    "code": account.code,
                    "name": account.name,
                    "account_type": account.account_type,
                }
            ),
            201,
        )


@api_bp.post("/journal-entries")
def create_journal_entry_via_api():
    payload = request.get_json(silent=True) or {}

    try:
        company_id = int(payload.get("company_id"))
        entry_date = date.fromisoformat(payload.get("entry_date"))
        description = (payload.get("description") or "").strip()
        status = (payload.get("status") or "posted").strip()
        raw_lines = payload.get("lines") or []

        if not description:
            return _validation_error(
                "Validation failed.",
                details=[{"field": "description", "message": "description is required."}],
            )
        if not isinstance(raw_lines, list):
            return _validation_error(
                "Validation failed.",
                details=[{"field": "lines", "message": "lines must be a list."}],
            )

        lines = []
        for idx, line in enumerate(raw_lines):
            if not isinstance(line, dict):
                return _validation_error(
                    "Validation failed.",
                    details=[
                        {
                            "field": f"lines[{idx}]",
                            "message": "line must be an object.",
                        }
                    ],
                )
            try:
                lines.append(
                    JournalLineInput(
                        account_id=int(line["account_id"]),
                        debit_amount=parse_decimal(str(line.get("debit_amount", "0.00"))),
                        credit_amount=parse_decimal(str(line.get("credit_amount", "0.00"))),
                        description=(line.get("description") or "").strip() or None,
                    )
                )
            except KeyError:
                return _validation_error(
                    "Validation failed.",
                    details=[
                        {
                            "field": f"lines[{idx}].account_id",
                            "message": "account_id is required.",
                        }
                    ],
                )
            except (TypeError, ValueError) as exc:
                return _validation_error(
                    "Validation failed.",
                    details=[
                        {
                            "field": f"lines[{idx}]",
                            "message": str(exc),
                        }
                    ],
                )

        entry_input = JournalEntryInput(
            company_id=company_id,
            entry_date=entry_date,
            description=description,
            status=status,
            lines=lines,
        )

        session_factory = _get_session_factory()
        with session_factory() as session:
            entry = create_journal_entry(session=session, payload=entry_input)

    except (JournalEntryValidationError, JournalEntryCreationError) as exc:
        return _validation_error(
            "Validation failed.",
            details=[{"field": "journal_entry", "message": str(exc)}],
        )
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid payload format."}), 400

    return (
        jsonify(
            {
                "id": entry.id,
                "posting_number": entry.posting_number,
                "created_at": entry.created_at.isoformat(),
            }
        ),
        201,
    )


@api_bp.get("/trial-balance")
def get_trial_balance():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        rows = trial_balance_for_company(session=session, company_id=company_id)

    return (
        jsonify(
            {
                "company_id": company_id,
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

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        report = income_statement_for_company(session=session, company_id=company_id)

    return (
        jsonify(
            {
                "company_id": company_id,
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

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        report = balance_sheet_for_company(session=session, company_id=company_id)

    totals = report["totals"]
    return (
        jsonify(
            {
                "company_id": company_id,
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


@api_bp.get("/exports/trial-balance.csv")
def export_trial_balance_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        rows = trial_balance_for_company(session=session, company_id=company_id)

    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["account_code", "account_name", "debit_total", "credit_total", "balance"])
    for row in rows:
        writer.writerow(
            [
                row["code"],
                row["name"],
                row["debit_total"],
                row["credit_total"],
                row["balance"],
            ]
        )

    file_name = f"trial-balance-company-{company_id}-{date.today().isoformat()}.csv"
    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@api_bp.get("/exports/journal.csv")
def export_journal_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        journal_rows = (
            session.query(
                JournalEntry.id,
                JournalEntry.posting_number,
                JournalEntry.entry_date,
                JournalEntry.description,
                JournalEntryLine.line_number,
                Account.code,
                Account.name,
                JournalEntryLine.debit_amount,
                JournalEntryLine.credit_amount,
            )
            .join(JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id)
            .join(Account, Account.id == JournalEntryLine.account_id)
            .filter(JournalEntry.company_id == company_id)
            .order_by(JournalEntry.entry_date, JournalEntry.id, JournalEntryLine.line_number)
            .all()
        )

    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(
        [
            "posting_number",
            "entry_date",
            "entry_description",
            "line_number",
            "account_code",
            "account_name",
            "debit_amount",
            "credit_amount",
        ]
    )
    for row in journal_rows:
        writer.writerow(
            [
                row.posting_number,
                row.entry_date.isoformat(),
                row.description,
                row.line_number,
                row.code,
                row.name,
                row.debit_amount,
                row.credit_amount,
            ]
        )

    file_name = f"journal-company-{company_id}-{date.today().isoformat()}.csv"
    return Response(
        csv_buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@api_bp.post("/mcp/call")
def mcp_call():
    payload = request.get_json(silent=True) or {}
    method = (payload.get("method") or "").strip()
    params = payload.get("params") or {}
    request_id = payload.get("id", "openbuchhaltung-mcp-call")

    if not method:
        return jsonify({"error": "method is required."}), 400

    if not isinstance(params, dict):
        return jsonify({"error": "params must be an object."}), 400

    server_url = current_app.config.get("MCP_SERVER_URL")
    try:
        mcp_response = call_mcp_server(
            server_url=server_url,
            method=method,
            params=params,
            request_id=request_id,
        )
    except MCPError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(mcp_response), 200
