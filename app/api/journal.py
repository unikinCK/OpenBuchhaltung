"""Journalbuchungen über die API."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
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
    finalize_journal_entry,
    parse_decimal,
    reverse_journal_entry,
)
from domain.models import JournalEntry
from domain.services.journal_entry_validation import JournalEntryValidationError


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
                lines.append(
                    JournalLineInput(
                        account_id=int(raw_account_id) if raw_account_id is not None else None,
                        account_code=str(raw_account_code).strip() if has_account_code else None,
                        debit_amount=parse_decimal(str(line.get("debit_amount", "0.00"))),
                        credit_amount=parse_decimal(str(line.get("credit_amount", "0.00"))),
                        description=(line.get("description") or "").strip() or None,
                        tax_code_id=int(raw_tax_code_id) if raw_tax_code_id is not None else None,
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
