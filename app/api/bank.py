"""Bankumsätze über die API."""

from __future__ import annotations

import base64
import binascii
from io import StringIO
from pathlib import Path

from flask import jsonify, request
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.bank_import import (
    BankImportError,
    BankImportReport,
    book_transaction,
    import_bank_csv,
    match_transaction,
    suggest_matches,
)
from app.services.journal_entries import JournalEntryCreationError
from app.services.scoping import scoped_select
from domain.models import Account, BankTransaction, JournalEntry
from domain.services.journal_entry_validation import JournalEntryValidationError

ALLOWED_BANK_CSV_MIME_TYPES = {"text/csv", "application/vnd.ms-excel", "application/octet-stream"}


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _transaction_dict(transaction: BankTransaction) -> dict[str, object]:
    return {
        "id": transaction.id,
        "tenant_id": transaction.tenant_id,
        "company_id": transaction.company_id,
        "bank_account_id": transaction.bank_account_id,
        "booking_date": transaction.booking_date.isoformat(),
        "amount": str(transaction.amount),
        "currency_code": transaction.currency_code,
        "purpose": transaction.purpose,
        "counterparty": transaction.counterparty,
        "status": transaction.status,
        "journal_entry_id": transaction.journal_entry_id,
        "imported_at": transaction.imported_at.isoformat(),
    }


def _journal_entry_suggestion_dict(entry: JournalEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "posting_number": entry.posting_number,
        "entry_date": entry.entry_date.isoformat(),
        "description": entry.description,
    }


def _report_dict(report: BankImportReport) -> dict[str, object]:
    return {
        "total_rows": report.total_rows,
        "imported_rows": report.imported_rows,
        "duplicate_rows": report.duplicate_rows,
        "error_rows": report.error_rows,
        "errors": [
            {"line_number": error.line_number, "message": error.message}
            for error in report.errors
        ],
    }


def _load_import_payload():
    if request.files:
        uploaded_file = request.files.get("bank_csv")
        if uploaded_file is None or not uploaded_file.filename:
            return None, jsonify({"error": "bank_csv is required."}), 400
        return (
            {
                "company_id": request.form.get("company_id", type=int),
                "bank_account_id": request.form.get("bank_account_id", type=int),
                "file_name": secure_filename(uploaded_file.filename),
                "mime_type": uploaded_file.mimetype or "text/csv",
                "content": uploaded_file.read(),
            },
            None,
            None,
        )

    payload = request.get_json(silent=True) or {}
    content_base64 = (payload.get("content_base64") or "").strip()
    if not content_base64:
        return None, jsonify({"error": "content_base64 is required."}), 400
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        return None, jsonify({"error": "content_base64 must be valid base64."}), 400
    return (
        {
            "company_id": payload.get("company_id"),
            "bank_account_id": payload.get("bank_account_id"),
            "file_name": secure_filename((payload.get("file_name") or "").strip()),
            "mime_type": payload.get("mime_type") or "text/csv",
            "content": content,
        },
        None,
        None,
    )


def _validate_csv_upload(*, file_name: str, mime_type: str) -> str | None:
    if not file_name:
        return "file_name is required."
    if Path(file_name).suffix.lower() != ".csv":
        return "Only CSV bank files may be imported."
    if mime_type not in ALLOWED_BANK_CSV_MIME_TYPES:
        return "Bank CSV MIME type is not allowed."
    return None


@api_bp.get("/bank-transactions")
def list_bank_transactions_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    status = (request.args.get("status") or "").strip() or None
    include_suggestions = (request.args.get("include_suggestions") or "").lower() in {
        "1",
        "true",
        "yes",
    }

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = scoped_select(BankTransaction, company_id=company_id).order_by(
            BankTransaction.booking_date.desc(), BankTransaction.id.desc()
        )
        if status is not None:
            stmt = stmt.where(BankTransaction.status == status)
        transactions = session.execute(stmt).scalars().all()

        payload = []
        for transaction in transactions:
            transaction_payload = _transaction_dict(transaction)
            if include_suggestions and transaction.status == "open":
                transaction_payload["suggestions"] = [
                    _journal_entry_suggestion_dict(entry)
                    for entry in suggest_matches(session=session, transaction=transaction)
                ]
            payload.append(transaction_payload)

        return jsonify({"company_id": company_id, "transactions": payload}), 200


@api_bp.post("/bank-transactions/import")
def import_bank_transactions_via_api():
    if not api_can_write():
        return forbidden()

    upload, error_response, status_code = _load_import_payload()
    if error_response is not None:
        return error_response, status_code

    try:
        company_id = int(upload["company_id"])
        bank_account_id = int(upload["bank_account_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and bank_account_id are required."}), 400

    validation_error = _validate_csv_upload(
        file_name=upload["file_name"], mime_type=upload["mime_type"]
    )
    if validation_error:
        return jsonify({"error": validation_error}), 422

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        bank_account = session.get(Account, bank_account_id)
        if bank_account is None or bank_account.company_id != company.id:
            return jsonify({"error": "Bank account not found."}), 404

        try:
            csv_text = upload["content"].decode("utf-8-sig")
            report = import_bank_csv(
                session=session,
                company_id=company.id,
                bank_account_id=bank_account.id,
                csv_stream=StringIO(csv_text),
                changed_by=_api_changed_by(),
            )
        except UnicodeDecodeError:
            return jsonify({"error": "CSV must be UTF-8 encoded."}), 422
        except BankImportError as exc:
            return jsonify({"error": str(exc)}), 422

        transactions = (
            session.execute(
                scoped_select(BankTransaction, company_id=company.id)
                .where(BankTransaction.bank_account_id == bank_account.id)
                .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
            )
            .scalars()
            .all()
        )
        return (
            jsonify(
                {
                    "company_id": company.id,
                    "bank_account_id": bank_account.id,
                    "report": _report_dict(report),
                    "transactions": [
                        _transaction_dict(transaction) for transaction in transactions
                    ],
                }
            ),
            201,
        )


@api_bp.post("/bank-transactions/<int:transaction_id>/match")
def match_bank_transaction_via_api(transaction_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        journal_entry_id = int(payload.get("journal_entry_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "journal_entry_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        transaction = session.get(BankTransaction, transaction_id)
        if transaction is None or api_scoped_company(session, transaction.company_id) is None:
            return jsonify({"error": "Bank transaction not found."}), 404
        try:
            transaction = match_transaction(
                session=session,
                transaction_id=transaction.id,
                journal_entry_id=journal_entry_id,
                changed_by=_api_changed_by(),
            )
        except BankImportError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_transaction_dict(transaction)), 200


@api_bp.post("/bank-transactions/<int:transaction_id>/book")
def book_bank_transaction_via_api(transaction_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        contra_account_id = int(payload.get("contra_account_id"))
        tax_code_id = (
            int(payload["tax_code_id"]) if payload.get("tax_code_id") is not None else None
        )
        cost_center_id = (
            int(payload["cost_center_id"])
            if payload.get("cost_center_id") not in (None, "")
            else None
        )
        profit_center_id = (
            int(payload["profit_center_id"])
            if payload.get("profit_center_id") not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "contra_account_id must be an integer."}), 400
    description = (payload.get("description") or "").strip() or None

    session_factory = get_session_factory()
    with session_factory() as session:
        transaction = session.get(BankTransaction, transaction_id)
        if transaction is None or api_scoped_company(session, transaction.company_id) is None:
            return jsonify({"error": "Bank transaction not found."}), 404
        try:
            transaction = book_transaction(
                session=session,
                transaction_id=transaction.id,
                contra_account_id=contra_account_id,
                tax_code_id=tax_code_id,
                description=description,
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
                changed_by=_api_changed_by(),
            )
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_transaction_dict(transaction)), 201
