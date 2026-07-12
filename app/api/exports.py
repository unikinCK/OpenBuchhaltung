"""Exporte: Journal-/SuSa-CSV und DATEV-Buchungsstapel."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from io import StringIO

from flask import Response, current_app, jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_scoped_company, get_session_factory
from app.services.datev_export import DatevExportOptions, build_datev_export
from app.services.reports import trial_balance_for_company
from domain.models import Account, JournalEntry, JournalEntryLine


@api_bp.get("/exports/trial-balance.csv")
def export_trial_balance_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
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

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
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


@api_bp.get("/exports/datev.csv")
def export_datev_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        options = DatevExportOptions(
            consultant_number=current_app.config.get("DATEV_CONSULTANT_NUMBER") or 1000,
            client_number=current_app.config.get("DATEV_CLIENT_NUMBER") or company_id,
        )
        content = build_datev_export(
            session=session,
            company_id=company_id,
            options=options,
            generated_at=datetime.now(timezone.utc),
        )

    # DATEV erwartet Windows-1252; nicht abbildbare Zeichen werden ersetzt.
    payload = content.encode("cp1252", errors="replace")
    file_name = f"EXTF_Buchungsstapel_{company_id}_{date.today().isoformat()}.csv"
    return Response(
        payload,
        content_type="text/csv; charset=windows-1252",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
