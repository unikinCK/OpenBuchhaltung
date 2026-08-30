"""Bankumsätze über die API."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from flask import jsonify, request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.accounts import create_account_with_audit, serialize_account
from app.services.bank_import import (
    BankImportError,
    BankImportReport,
    bank_reconciliation,
    book_transaction,
    detect_transfer_counterparts,
    find_geldtransit_account,
    import_bank_statement,
    match_transaction,
    move_bank_transactions,
    reassign_bank_transactions,
    suggest_matches_for,
)
from app.services.journal_entries import JournalEntryCreationError
from app.services.scoping import scoped_select
from domain.models import Account, BankTransaction, JournalEntry
from domain.services.journal_entry_validation import JournalEntryValidationError

DEFAULT_TRANSACTION_PAGE_SIZE = 200

ALLOWED_BANK_FILE_SUFFIXES = {".csv", ".xml", ".sta", ".mt940", ".940"}
ALLOWED_BANK_FILE_MIME_TYPES = {
    "text/csv",
    "text/plain",
    "text/xml",
    "application/xml",
    "application/vnd.ms-excel",
    "application/octet-stream",
}


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
        "bank_reference": transaction.bank_reference,
        "status": transaction.status,
        "journal_entry_id": transaction.journal_entry_id,
        "imported_at": transaction.imported_at.isoformat(),
    }


def _scoped_transaction_ids(
    session, *, company_id: int, transaction_ids: list[int]
) -> list[int]:
    """Stellt sicher, dass alle Umsatz-IDs zur adressierten Gesellschaft gehören."""
    known = set(
        session.execute(
            scoped_select(BankTransaction, company_id=company_id).where(
                BankTransaction.id.in_(transaction_ids)
            )
        )
        .scalars()
        .all()
    )
    known_ids = {transaction.id for transaction in known}
    foreign = [str(value) for value in transaction_ids if value not in known_ids]
    if foreign:
        raise BankImportError(f"Bankumsatz nicht gefunden: {', '.join(foreign)}")
    return transaction_ids


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
        uploaded_file = request.files.get("bank_file") or request.files.get("bank_csv")
        if uploaded_file is None or not uploaded_file.filename:
            return None, jsonify({"error": "bank_file is required."}), 400
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


def _validate_statement_upload(*, file_name: str, mime_type: str) -> str | None:
    if not file_name:
        return "file_name is required."
    if Path(file_name).suffix.lower() not in ALLOWED_BANK_FILE_SUFFIXES:
        return "Only CSV, CAMT.053 (XML) or MT940 bank files may be imported."
    if mime_type not in ALLOWED_BANK_FILE_MIME_TYPES:
        return "Bank file MIME type is not allowed."
    return None


@api_bp.get("/bank-accounts")
def list_bank_accounts_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = scoped_select(Account, company_id=company.id).where(
            Account.account_type == "asset"
        )
        if not include_inactive:
            stmt = stmt.where(Account.is_active.is_(True))
        accounts = session.execute(stmt.order_by(Account.code)).scalars().all()

        return (
            jsonify(
                {
                    "company_id": company_id,
                    "bank_accounts": [serialize_account(account) for account in accounts],
                }
            ),
            200,
        )


@api_bp.post("/bank-accounts")
def create_bank_account_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()

    if not company_id or not code or not name:
        return jsonify({"error": "company_id, code and name are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        try:
            account = create_account_with_audit(
                session=session,
                company=company,
                code=code,
                name=name,
                account_type="asset",
                changed_by=_api_changed_by(),
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Account code already exists for this company."}), 409

        return jsonify(serialize_account(account)), 201


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
    limit = request.args.get("limit", type=int)
    limit = DEFAULT_TRANSACTION_PAGE_SIZE if limit is None else max(1, min(limit, 1000))
    offset = max(0, request.args.get("offset", type=int) or 0)
    query = (request.args.get("q") or "").strip() or None
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = scoped_select(BankTransaction, company_id=company_id)
        if status is not None:
            stmt = stmt.where(BankTransaction.status == status)
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                BankTransaction.purpose.ilike(pattern)
                | BankTransaction.counterparty.ilike(pattern)
                | BankTransaction.bank_reference.ilike(pattern)
            )
        if date_from is not None:
            stmt = stmt.where(BankTransaction.booking_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(BankTransaction.booking_date <= date_to)
        total = session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        transactions = (
            session.execute(
                stmt.order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )

        suggestions_by_tx = (
            suggest_matches_for(session=session, transactions=transactions)
            if include_suggestions
            else {}
        )
        transfer_by_tx = (
            detect_transfer_counterparts(session=session, transactions=transactions)
            if include_suggestions
            else {}
        )
        geldtransit_account = (
            find_geldtransit_account(session=session, company_id=company_id)
            if include_suggestions
            else None
        )
        payload = []
        for transaction in transactions:
            transaction_payload = _transaction_dict(transaction)
            if include_suggestions and transaction.status == "open":
                transaction_payload["suggestions"] = [
                    _journal_entry_suggestion_dict(entry)
                    for entry in suggestions_by_tx.get(transaction.id, [])
                ]
                counterpart = transfer_by_tx.get(transaction.id)
                transaction_payload["transfer_counterpart_id"] = (
                    counterpart.id if counterpart else None
                )
            payload.append(transaction_payload)

        response_body: dict[str, object] = {
            "company_id": company_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "transactions": payload,
        }
        if include_suggestions:
            response_body["geldtransit_account_id"] = (
                geldtransit_account.id if geldtransit_account else None
            )
        return jsonify(response_body), 200


@api_bp.get("/bank-reconciliation")
def bank_reconciliation_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        rows = bank_reconciliation(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "accounts": [
                        {
                            "account_id": row.account_id,
                            "account_code": row.account_code,
                            "account_name": row.account_name,
                            "book_balance": str(row.book_balance),
                            "statement_total": str(row.statement_total),
                            "difference": str(row.difference),
                        }
                        for row in rows
                    ],
                }
            ),
            200,
        )


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

    validation_error = _validate_statement_upload(
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
            report = import_bank_statement(
                session=session,
                company_id=company.id,
                bank_account_id=bank_account.id,
                file_name=upload["file_name"],
                content=upload["content"],
                changed_by=_api_changed_by(),
            )
        except BankImportError as exc:
            return jsonify({"error": str(exc)}), 422

        transactions = (
            session.execute(
                scoped_select(BankTransaction, company_id=company.id)
                .where(BankTransaction.bank_account_id == bank_account.id)
                .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
                .limit(DEFAULT_TRANSACTION_PAGE_SIZE)
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


@api_bp.post("/bank-transactions/reassign")
def reassign_bank_transactions_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        target_bank_account_id = int(payload.get("target_bank_account_id"))
    except (TypeError, ValueError):
        return (
            jsonify({"error": "company_id and target_bank_account_id are required."}),
            400,
        )

    source_bank_account_id = payload.get("source_bank_account_id")
    transaction_ids = payload.get("transaction_ids")
    if (source_bank_account_id is None) == (transaction_ids is None):
        return (
            jsonify(
                {
                    "error": (
                        "Exactly one of source_bank_account_id or transaction_ids is required."
                    )
                }
            ),
            400,
        )

    statuses = payload.get("statuses") or None
    reclassify = bool(payload.get("reclassify", True))
    try:
        if source_bank_account_id is not None:
            source_bank_account_id = int(source_bank_account_id)
        else:
            transaction_ids = [int(value) for value in transaction_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "Transaction and account ids must be integers."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        try:
            if source_bank_account_id is not None:
                result = move_bank_transactions(
                    session=session,
                    company_id=company.id,
                    source_bank_account_id=source_bank_account_id,
                    target_bank_account_id=target_bank_account_id,
                    statuses=statuses,
                    changed_by=_api_changed_by(),
                    reclassify=reclassify,
                )
            else:
                result = reassign_bank_transactions(
                    session=session,
                    transaction_ids=_scoped_transaction_ids(
                        session, company_id=company.id, transaction_ids=transaction_ids
                    ),
                    bank_account_id=target_bank_account_id,
                    changed_by=_api_changed_by(),
                    reclassify=reclassify,
                )
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            return jsonify({"error": str(exc)}), 422

        entry = result.reclassification_entry
        return (
            jsonify(
                {
                    "company_id": company.id,
                    "target_bank_account_id": target_bank_account_id,
                    "reassigned_count": len(result.transactions),
                    "reclassification_entry": (
                        {"id": entry.id, "posting_number": entry.posting_number}
                        if entry
                        else None
                    ),
                    "transactions": [
                        _transaction_dict(transaction) for transaction in result.transactions
                    ],
                }
            ),
            200,
        )


@api_bp.post("/bank-transactions/<int:transaction_id>/bank-account")
def set_bank_transaction_account_via_api(transaction_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        bank_account_id = int(payload.get("bank_account_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "bank_account_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        transaction = session.get(BankTransaction, transaction_id)
        if transaction is None or api_scoped_company(session, transaction.company_id) is None:
            return jsonify({"error": "Bank transaction not found."}), 404
        try:
            result = reassign_bank_transactions(
                session=session,
                transaction_ids=[transaction.id],
                bank_account_id=bank_account_id,
                changed_by=_api_changed_by(),
                reclassify=bool(payload.get("reclassify", True)),
            )
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            return jsonify({"error": str(exc)}), 422
        entry = result.reclassification_entry
        transaction_payload = _transaction_dict(
            result.transactions[0] if result.transactions else transaction
        )
        transaction_payload["reclassification_entry"] = (
            {"id": entry.id, "posting_number": entry.posting_number} if entry else None
        )
        return jsonify(transaction_payload), 200


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
