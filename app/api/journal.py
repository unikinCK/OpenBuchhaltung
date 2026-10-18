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
from app.services.controlling import (
    ControllingError,
    find_filter_unit,
    journal_line_filter,
    parse_unit_filter,
)
from app.services.documents import documents_by_journal_entry
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    amend_journal_entry,
    create_journal_entry,
    finalize_journal_entries_until,
    finalize_journal_entry,
    parse_decimal,
    reverse_journal_entry,
)
from app.services.partners import find_partner_by_number, partner_display_number
from domain.models import Account, JournalEntry, JournalEntryLine
from domain.services.journal_entry_validation import JournalEntryValidationError


def _entry_partners(lines) -> list[dict[str, object]]:
    """Geschäftspartner der Buchung: verschiedene Partner der Zeilen in Zeilenfolge."""
    partners: dict[int, dict[str, object]] = {}
    for line in lines:
        if line.partner is not None and line.partner_id not in partners:
            partners[line.partner_id] = {
                "id": line.partner_id,
                "name": line.partner.name,
                "number": partner_display_number(line.partner, line.account.subledger),
            }
    return list(partners.values())


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
        "service_date": entry.service_date.isoformat() if entry.service_date else None,
        "description": entry.description,
        "source": entry.source,
        "is_finalized": entry.is_finalized,
        "finalized_at": entry.finalized_at.isoformat() if entry.finalized_at else None,
        "finalized_by": entry.finalized_by,
        "content_hash_version": entry.content_hash_version,
        "content_hash": entry.content_hash,
        "reversal_of_id": entry.reversal_of_id,
        "created_at": entry.created_at.isoformat(),
        "partners": _entry_partners(lines),
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
                "partner_id": line.partner_id,
                "partner_number": (
                    partner_display_number(line.partner, line.account.subledger)
                    if line.partner
                    else None
                ),
                "partner_name": line.partner.name if line.partner else None,
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
    partner_id_raw = (request.args.get("partner_id") or "").strip()
    if partner_id_raw and not partner_id_raw.isdigit():
        return jsonify({"error": "partner_id must be an integer."}), 400
    partner_id = int(partner_id_raw) if partner_id_raw else None
    account_id_raw = (request.args.get("account_id") or "").strip()
    if account_id_raw and not account_id_raw.isdigit():
        return jsonify({"error": "account_id must be an integer."}), 400
    account_id = int(account_id_raw) if account_id_raw else None
    unit_filters: dict[str, int | str | None] = {}
    try:
        for unit_type in ("cost_center", "profit_center"):
            unit_filters[unit_type] = parse_unit_filter(
                request.args.get(f"{unit_type}_id"), unit_type=unit_type
            )
    except ControllingError as exc:
        return jsonify({"error": str(exc)}), 400

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
        if account_id is not None:
            account = session.get(Account, account_id)
            if account is None or account.company_id != company_id:
                return jsonify({"error": "Account not found."}), 404
        for unit_type, value in unit_filters.items():
            if isinstance(value, int) and (
                find_filter_unit(
                    session, company_id=company_id, unit_type=unit_type, unit_id=value
                )
                is None
            ):
                return jsonify({"error": f"{unit_type}_id not found."}), 404

        stmt = (
            select(JournalEntry)
            .where(JournalEntry.company_id == company_id)
            .options(
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.account),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.tax_code),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.cost_center),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.profit_center),
                selectinload(JournalEntry.lines).selectinload(JournalEntryLine.partner),
            )
            .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        if date_from is not None:
            stmt = stmt.where(JournalEntry.entry_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(JournalEntry.entry_date <= date_to)
        query = (request.args.get("q") or "").strip() or None
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                JournalEntry.description.ilike(pattern)
                | JournalEntry.posting_number.ilike(pattern)
            )
        if source:
            stmt = stmt.where(JournalEntry.source == source)
        if is_finalized is not None:
            stmt = stmt.where(JournalEntry.is_finalized.is_(is_finalized))
        if partner_id is not None:
            stmt = stmt.where(
                JournalEntry.id.in_(
                    select(JournalEntryLine.journal_entry_id).where(
                        JournalEntryLine.partner_id == partner_id
                    )
                )
            )
        line_filter = journal_line_filter(
            account_id=account_id,
            cost_center=unit_filters["cost_center"],
            profit_center=unit_filters["profit_center"],
        )
        if line_filter is not None:
            stmt = stmt.where(JournalEntry.id.in_(line_filter))

        entries = session.execute(stmt).scalars().all()
        documents_by_entry = documents_by_journal_entry(
            session, [entry.id for entry in entries]
        )

    return (
        jsonify(
            {
                "company_id": company_id,
                "period": {
                    "date_from": date_from.isoformat() if date_from else None,
                    "date_to": date_to.isoformat() if date_to else None,
                },
                "limit": max(1, min(limit, 500)),
                "partner_id": partner_id,
                "account_id": account_id,
                "cost_center_id": unit_filters["cost_center"],
                "profit_center_id": unit_filters["profit_center"],
                "entries": [
                    {
                        **_journal_entry_dict(entry),
                        "documents": [
                            {
                                **document,
                                "document_date": (
                                    document["document_date"].isoformat()
                                    if document["document_date"]
                                    else None
                                ),
                            }
                            for document in documents_by_entry.get(entry.id, [])
                        ],
                    }
                    for entry in entries
                ],
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
        raw_service_date = payload.get("service_date")
        try:
            service_date = (
                date.fromisoformat(str(raw_service_date).strip())
                if raw_service_date not in (None, "")
                else None
            )
        except ValueError:
            return validation_error(
                "Validation failed.",
                details=[
                    {
                        "field": "service_date",
                        "message": "service_date must be an ISO date (YYYY-MM-DD).",
                    }
                ],
            )

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
                raw_partner_id = line.get("partner_id")
                raw_partner_number = line.get("partner_number")
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
                        partner_id=int(raw_partner_id) if raw_partner_id is not None else None,
                        partner_number=(
                            str(raw_partner_number).strip()
                            if raw_partner_number is not None
                            else None
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

        raw_entry_partner_id = payload.get("partner_id")
        raw_entry_partner_number = payload.get("partner_number")
        entry_input = JournalEntryInput(
            company_id=company_id,
            entry_date=entry_date,
            description=description,
            status=status,
            lines=lines,
            changed_by=_api_changed_by(),
            service_date=service_date,
            partner_id=(
                int(raw_entry_partner_id) if raw_entry_partner_id not in (None, "") else None
            ),
            partner_number=(
                str(raw_entry_partner_number).strip()
                if raw_entry_partner_number not in (None, "")
                else None
            ),
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
                "service_date": entry.service_date.isoformat() if entry.service_date else None,
                "created_at": entry.created_at.isoformat(),
            }
        ),
        201,
    )


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


AMEND_LINE_FIELDS = ("partner_id", "partner_number", "cost_center_id", "profit_center_id")


def _amend_lines(session, company_id: int, raw_lines) -> tuple[dict, dict, list]:
    """Änderungen je Zeile aus ``lines``.

    Liefert Zeilennummer → Partner-ID (``partner_id`` oder ``partner_number``),
    die Kontierung je Einheitentyp (``cost_center_id``/``profit_center_id``) und
    Fehler. Fehlende Felder bleiben unverändert, ``null`` entfernt den Wert.
    """
    line_partners: dict[int, int | None] = {}
    line_units: dict[str, dict[int, int | None]] = {"cost_center": {}, "profit_center": {}}
    errors = []
    if not isinstance(raw_lines, list):
        return {}, line_units, [{"field": "lines", "message": "lines must be a list."}]
    for idx, line in enumerate(raw_lines):
        field = f"lines[{idx}]"
        if not isinstance(line, dict):
            errors.append({"field": field, "message": "line must be an object."})
            continue
        try:
            line_number = int(line.get("line_number"))
        except (TypeError, ValueError):
            errors.append({"field": f"{field}.line_number", "message": "line_number is required."})
            continue
        if not any(key in line for key in AMEND_LINE_FIELDS):
            errors.append(
                {
                    "field": field,
                    "message": (
                        "partner_id, partner_number, cost_center_id or profit_center_id "
                        "required (null entfernt den Wert)."
                    ),
                }
            )
            continue
        for unit_type in ("cost_center", "profit_center"):
            key = f"{unit_type}_id"
            if key not in line:
                continue
            raw_unit = line.get(key)
            try:
                line_units[unit_type][line_number] = (
                    int(raw_unit) if raw_unit not in (None, "") else None
                )
            except (TypeError, ValueError):
                errors.append(
                    {"field": f"{field}.{key}", "message": f"{key} must be an integer or null."}
                )
        raw_number = line.get("partner_number")
        if raw_number not in (None, ""):
            partner = find_partner_by_number(
                session=session, company_id=company_id, number=str(raw_number).strip()
            )
            if partner is None:
                errors.append(
                    {
                        "field": f"{field}.partner_number",
                        "message": f"Geschäftspartner mit Nummer {raw_number} nicht gefunden.",
                    }
                )
                continue
            line_partners[line_number] = partner.id
        elif "partner_id" in line:
            raw_id = line.get("partner_id")
            try:
                line_partners[line_number] = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                errors.append(
                    {"field": f"{field}.partner_id", "message": "partner_id must be an integer."}
                )
    return line_partners, line_units, errors


@api_bp.patch("/journal-entries/<int:journal_entry_id>")
def amend_journal_entry_via_api(journal_entry_id: int):
    """Offene Buchung ergänzen: Leistungsdatum, Geschäftspartner (Buchung/Zeile) sowie
    Kostenstelle und Profitcenter je Zeile."""
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object expected."}), 400
    options: dict[str, object] = {}
    if "service_date" in payload:
        raw_service_date = payload.get("service_date")
        try:
            options["service_date"] = (
                date.fromisoformat(str(raw_service_date).strip())
                if raw_service_date not in (None, "")
                else None
            )
        except ValueError:
            return validation_error(
                "Validation failed.",
                details=[
                    {
                        "field": "service_date",
                        "message": "service_date must be an ISO date (YYYY-MM-DD) or null.",
                    }
                ],
            )

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = session.get(JournalEntry, journal_entry_id)
        if entry is None or api_scoped_company(session, entry.company_id) is None:
            return jsonify({"error": "Journal entry not found."}), 404
        line_partners, line_units, errors = _amend_lines(
            session, entry.company_id, payload.get("lines") or []
        )
        raw_number = payload.get("partner_number")
        if raw_number not in (None, ""):
            partner = find_partner_by_number(
                session=session, company_id=entry.company_id, number=str(raw_number).strip()
            )
            if partner is None:
                errors.append(
                    {
                        "field": "partner_number",
                        "message": f"Geschäftspartner mit Nummer {raw_number} nicht gefunden.",
                    }
                )
            else:
                options["partner_id"] = partner.id
        elif "partner_id" in payload:
            raw_id = payload.get("partner_id")
            try:
                options["partner_id"] = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                errors.append(
                    {"field": "partner_id", "message": "partner_id must be an integer or null."}
                )
        if errors:
            return validation_error("Validation failed.", details=errors)
        try:
            entry = amend_journal_entry(
                session=session,
                company_id=entry.company_id,
                journal_entry_id=journal_entry_id,
                changed_by=_api_changed_by(),
                line_partners=line_partners,
                line_cost_centers=line_units["cost_center"],
                line_profit_centers=line_units["profit_center"],
                **options,
            )
        except JournalEntryCreationError as exc:
            return validation_error(
                "Validation failed.",
                details=[{"field": "journal_entry", "message": str(exc)}],
            )
        return jsonify(_journal_entry_dict(entry)), 200


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
