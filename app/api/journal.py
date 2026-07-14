"""Journalbuchungen über die API."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_user
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entries_until,
    finalize_journal_entry,
    parse_decimal,
    reverse_journal_entry,
)
from domain.models import JournalEntry, JournalEntryLine
from domain.services.journal_entry_validation import JournalEntryValidationError


def _journal_entry_dict(entry: JournalEntry) -> dict[str, object]:
    lines = sorted(entry.lines, key=lambda line: line.line_number)
    return {
        "id": entry.id,
        "tenant_id": entry.tenant_id,
        "company_id": entry.company_id,
        "fiscal_year_id": entry.fiscal_year_id,
        "period_id": entry.period_id,
        "posting_number": entry.posting_number,
        "entry_date": entry.entry_date.isoformat(),
        "description": entry.description,
        "source": entry.source,
        "is_finalized": entry.is_finalized,
        "finalized_at": entry.finalized_at.isoformat() if entry.finalized_at else None,
        "finalized_by": entry.finalized_by,
        "content_hash_version": entry.content_hash_version,
        "content_hash": entry.content_hash,
        "reversal_of_id": entry.reversal_of_id,
        "created_at": entry.created_at.isoformat(),
        "lines": [
            {
                "id": line.id,
                "line_number": line.line_number,
                "account_id": line.account_id,
                "account_code": line.account.code,
                "account_name": line.account.name,
                "tax_code_id": line.tax_code_id,
                "tax_code": line.tax_code.code if line.tax_code else None,
                "cost_center_id": line.cost_center_id,
                "cost_center_code": line.cost_center.code if line.cost_center else None,
                "profit_center_id": line.profit_center_id,
                "profit_center_code": line.profit_center.code if line.profit_center else None,
                "description": line.description,
                "debit_amount": str(line.debit_amount),
                "credit_amount": str(line.credit_amount),
                "currency_code": line.currency_code,
            }
            for line in lines
        ],
    }


@api_bp.get("/journal-entries")
def list_journal_entries_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    source = (request.args.get("source") or "").strip() or None
    is_finalized_raw = request.args.get("is_finalized")
    limit = request.args.get("limit", default=100, type=int)
    if limit is None:
        return jsonify({"error": "limit must be an integer."}), 400

    is_finalized = None
    if is_finalized_raw is not None and is_finalized_raw.strip() != "":
        normalized = is_finalized_raw.strip().lower()
        if normalized not in {"true", "false", "1", "0"}:
            return jsonify({"error": "is_finalized must be true or false."}), 400
        is_finalized = normalized in {"true", "1"}

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = (
            select(JournalEntry)
            .where(JournalEntry.company_id == company_id)
            .options(
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.account),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.tax_code),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.cost_center),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.profit_center),
            )
            .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        if date_from is not None:
            stmt = stmt.where(JournalEntry.entry_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(JournalEntry.entry_date <= date_to)
        if source:
            stmt = stmt.where(JournalEntry.source == source)
        if is_finalized is not None:
            stmt = stmt.where(JournalEntry.is_finalized.is_(is_finalized))

        entries = session.execute(stmt).scalars().all()

    return (
        jsonify(
            {
                "company_id": company_id,
                "period": {
                    "date_from": date_from.isoformat() if date_from else None,
                    "date_to": date_to.isoformat() if date_to else None,
                },
                "limit": max(1, min(limit, 500)),
                "entries": [_journal_entry_dict(entry) for entry in entries],
            }
        ),
        200,
    )


@api_bp.post("/journal-entries")
def create_journal_entry_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}

    try:
        company_id = int(payload.get("company_id"))
        entry_date = date.fromisoformat(payload.get("entry_date"))
        description = (payload.get("description") or "").strip()
        status = (payload.get("status") or "posted").strip()
        raw_lines = payload.get("lines") or []

        if not description:
            return validation_error(
                "Validation failed.",
                details=[{"field": "description", "message": "description is required."}],
            )
        if not isinstance(raw_lines, list):
            return validation_error(
                "Validation failed.",
                details=[{"field": "lines", "message": "lines must be a list."}],
            )

        lines = []
        for idx, line in enumerate(raw_lines):
            if not isinstance(line, dict):
                return validation_error(
                    "Validation failed.",
                    details=[
                        {
                            "field": f"lines[{idx}]",
                            "message": "line must be an object.",
                        }
                    ],
                )
            raw_account_id = line.get("account_id")
            raw_account_code = line.get("account_code")
            has_account_code = raw_account_code is not None and str(raw_account_code).strip() != ""
            if raw_account_id is None and not has_account_code:
                return validation_error(
                    "Validation failed.",
                    details=[
                        {
                            "field": f"lines[{idx}].account_id",
                            "message": "account_id or account_code is required.",
                        }
                    ],
                )
            try:
                raw_tax_code_id = line.get("tax_code_id")
                raw_cost_center_id = line.get("cost_center_id")
                raw_profit_center_id = line.get("profit_center_id")
                lines.append(
                    JournalLineInput(
                        account_id=int(raw_account_id) if raw_account_id is not None else None,
                        account_code=str(raw_account_code).strip() if has_account_code else None,
                        debit_amount=parse_decimal(str(line.get("debit_amount", "0.00"))),
                        credit_amount=parse_decimal(str(line.get("credit_amount", "0.00"))),
                        description=(line.get("description") or "").strip() or None,
                        tax_code_id=int(raw_tax_code_id) if raw_tax_code_id is not None else None,
                        cost_center_id=(
                            int(raw_cost_center_id) if raw_cost_center_id is not None else None
                        ),
                        profit_center_id=(
                            int(raw_profit_center_id) if raw_profit_center_id is not None else None
                        ),
                    )
                )
            except (TypeError, ValueError) as exc:
                return validation_error(
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

        session_factory = get_session_factory()
        with session_factory() as session:
            if api_scoped_company(session, company_id) is None:
                return jsonify({"error": "Company not found."}), 404
            entry = create_journal_entry(session=session, payload=entry_input)

    except (JournalEntryValidationError, JournalEntryCreationError) as exc:
        return validation_error(
            "Validation failed.",
            details=[{"field": "journal_entry", "message": str(exc)}],
        )
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid payload format."}), 400
    except IntegrityError:
        return (
            jsonify({"error": "Conflict while saving the journal entry. Please retry."}),
            409,
        )

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


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


@api_bp.post("/journal-entries/finalize-until")
def finalize_journal_entries_until_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        up_to_date = date.fromisoformat(payload.get("up_to_date"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and valid up_to_date are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            count = finalize_journal_entries_until(
                session=session,
                company_id=company_id,
                up_to_date=up_to_date,
                changed_by=_api_changed_by(),
            )
        except JournalEntryCreationError as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "up_to_date": up_to_date.isoformat(),
                    "finalized_count": count,
                }
            ),
            200,
        )


@api_bp.post("/journal-entries/<int:journal_entry_id>/finalize")
def finalize_journal_entry_via_api(journal_entry_id: int):
    if not api_can_write():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = session.get(JournalEntry, journal_entry_id)
        if entry is None or api_scoped_company(session, entry.company_id) is None:
            return jsonify({"error": "Journal entry not found."}), 404
        try:
            entry = finalize_journal_entry(
                session=session,
                journal_entry_id=journal_entry_id,
                changed_by=_api_changed_by(),
            )
        except JournalEntryCreationError as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                {
                    "id": entry.id,
                    "posting_number": entry.posting_number,
                    "is_finalized": entry.is_finalized,
                    "finalized_at": entry.finalized_at.isoformat(),
                    "finalized_by": entry.finalized_by,
                }
            ),
            200,
        )


@api_bp.post("/journal-entries/<int:journal_entry_id>/reverse")
def reverse_journal_entry_via_api(journal_entry_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    reversal_date_raw = (payload.get("reversal_date") or "").strip()
    try:
        reversal_date = (
            date.fromisoformat(reversal_date_raw) if reversal_date_raw else date.today()
        )
    except ValueError:
        return jsonify({"error": "reversal_date must be an ISO date (YYYY-MM-DD)."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = session.get(JournalEntry, journal_entry_id)
        if entry is None or api_scoped_company(session, entry.company_id) is None:
            return jsonify({"error": "Journal entry not found."}), 404
        try:
            reversal = reverse_journal_entry(
                session=session,
                journal_entry_id=journal_entry_id,
                reversal_date=reversal_date,
                changed_by=_api_changed_by(),
            )
        except (JournalEntryCreationError, JournalEntryValidationError) as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                {
                    "id": reversal.id,
                    "posting_number": reversal.posting_number,
                    "reversal_of_id": reversal.reversal_of_id,
                    "entry_date": reversal.entry_date.isoformat(),
                    "is_finalized": reversal.is_finalized,
                }
            ),
            201,
        )
