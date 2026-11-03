"""Kassenbuch über die REST-API (ADR-007)."""

from __future__ import annotations

from datetime import date

from flask import Response, jsonify, request

from app.api.blueprint import api_bp
from app.api.downloads import linkable_download
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    bool_arg,
    bool_payload,
    date_arg,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_user
from app.services.cash_book import (
    CashBookError,
    cash_report,
    cash_report_csv,
    cash_report_file_name,
    create_cash_book,
    create_cash_closing,
    create_cash_entry,
    list_cash_books,
    list_cash_closings,
    list_cash_entries,
    reversal_numbers,
    reverse_cash_entry,
    serialize_cash_book,
    serialize_cash_closing,
    serialize_cash_entry,
    update_cash_book,
    verify_cash_book_integrity,
)
from domain.models import CashBook

BOOK_CREATE_FIELDS = {
    "company_id",
    "account_id",
    "name",
    "opening_amount",
    "opening_date",
    "opening_contra_account_id",
}
BOOK_UPDATE_FIELDS = {"name", "is_active"}
ENTRY_FIELDS = {
    "entry_type",
    "entry_date",
    "amount",
    "description",
    "contra_account_id",
    "tax_code_id",
    "partner_id",
    "partner_number",
    "document_id",
}
CLOSING_FIELDS = {"closing_date", "counted_balance", "difference_account_id", "description"}


class _PayloadError(ValueError):
    """Ein Feld im Request ist formal ungültig (HTTP 400)."""


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _scoped_book(session, cash_book_id: int) -> CashBook | None:
    book = session.get(CashBook, cash_book_id)
    if book is None or api_scoped_company(session, book.company_id) is None:
        return None
    return book


def _book_not_found():
    return jsonify({"error": "Cash book not found."}), 404


def _payload(allowed: set[str]) -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise _PayloadError("Request body must be a JSON object.")
    unknown = set(payload) - allowed
    if unknown:
        raise _PayloadError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    return payload


def _optional_int(payload: dict, name: str) -> int | None:
    value = payload.get(name)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise _PayloadError(f"{name} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _PayloadError(f"{name} must be an integer.") from exc


def _optional_date(payload: dict, name: str) -> date | None:
    value = payload.get(name)
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise _PayloadError(f"{name} must be an ISO date (YYYY-MM-DD).") from exc


def _optional_text(payload: dict, name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _PayloadError(f"{name} must be a string.")
    return value


def _paging() -> tuple[int, int]:
    limit = request.args.get("limit", default=100, type=int)
    offset = request.args.get("offset", default=0, type=int)
    return max(1, min(limit, 500)), max(0, offset)


@api_bp.get("/cash-books")
def list_cash_books_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        books = list_cash_books(
            session=session,
            company_id=company_id,
            include_inactive=bool_arg("include_inactive"),
        )
        return jsonify(
            {
                "company_id": company_id,
                "cash_books": [serialize_cash_book(session, book) for book in books],
            }
        )


@api_bp.post("/cash-books")
def create_cash_book_via_api():
    if not api_can_write():
        return forbidden()
    try:
        payload = _payload(BOOK_CREATE_FIELDS)
        company_id = _optional_int(payload, "company_id")
        account_id = _optional_int(payload, "account_id")
        opening_contra_account_id = _optional_int(payload, "opening_contra_account_id")
        opening_date = _optional_date(payload, "opening_date")
        name = _optional_text(payload, "name")
    except _PayloadError as exc:
        return jsonify({"error": str(exc)}), 400
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            book = create_cash_book(
                session=session,
                company_id=company_id,
                account_id=account_id,
                name=name,
                opening_amount=payload.get("opening_amount"),
                opening_date=opening_date,
                opening_contra_account_id=opening_contra_account_id,
                changed_by=_api_changed_by(),
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        return jsonify(serialize_cash_book(session, book)), 201


@api_bp.get("/cash-books/<int:cash_book_id>")
def get_cash_book_via_api(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _scoped_book(session, cash_book_id)
        if book is None:
            return _book_not_found()
        return jsonify(serialize_cash_book(session, book))


@api_bp.patch("/cash-books/<int:cash_book_id>")
def update_cash_book_via_api(cash_book_id: int):
    if not api_can_write():
        return forbidden()
    try:
        payload = _payload(BOOK_UPDATE_FIELDS)
        name = _optional_text(payload, "name")
    except _PayloadError as exc:
        return jsonify({"error": str(exc)}), 400
    is_active = bool_payload(payload["is_active"]) if "is_active" in payload else None

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        try:
            book = update_cash_book(
                session=session,
                cash_book_id=cash_book_id,
                name=name,
                is_active=is_active,
                changed_by=_api_changed_by(),
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        return jsonify(serialize_cash_book(session, book))


@api_bp.get("/cash-books/<int:cash_book_id>/entries")
def list_cash_entries_via_api(cash_book_id: int):
    limit, offset = _paging()
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        entries, total = list_cash_entries(
            session=session,
            cash_book_id=cash_book_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        reversed_by = reversal_numbers(session, [entry.id for entry in entries])
        return jsonify(
            {
                "cash_book_id": cash_book_id,
                "total": total,
                "limit": limit,
                "offset": offset,
                "entries": [
                    serialize_cash_entry(entry, reversed_by_number=reversed_by.get(entry.id))
                    for entry in entries
                ],
            }
        )


@api_bp.post("/cash-books/<int:cash_book_id>/entries")
def create_cash_entry_via_api(cash_book_id: int):
    if not api_can_write():
        return forbidden()
    try:
        payload = _payload(ENTRY_FIELDS)
        entry_date = _optional_date(payload, "entry_date")
        contra_account_id = _optional_int(payload, "contra_account_id")
        tax_code_id = _optional_int(payload, "tax_code_id")
        partner_id = _optional_int(payload, "partner_id")
        document_id = _optional_int(payload, "document_id")
        entry_type = _optional_text(payload, "entry_type")
        description = _optional_text(payload, "description")
    except _PayloadError as exc:
        return jsonify({"error": str(exc)}), 400
    partner_number = str(payload.get("partner_number") or "").strip() or None

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        try:
            entry = create_cash_entry(
                session=session,
                cash_book_id=cash_book_id,
                entry_type=entry_type,
                entry_date=entry_date,
                amount=payload.get("amount"),
                description=description,
                contra_account_id=contra_account_id,
                tax_code_id=tax_code_id,
                partner_id=partner_id,
                partner_number=partner_number,
                document_id=document_id,
                changed_by=_api_changed_by(),
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        return jsonify(serialize_cash_entry(entry)), 201


@api_bp.post("/cash-books/<int:cash_book_id>/entries/<int:entry_id>/reverse")
def reverse_cash_entry_via_api(cash_book_id: int, entry_id: int):
    if not api_can_write():
        return forbidden()
    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        try:
            entry = reverse_cash_entry(
                session=session,
                cash_book_id=cash_book_id,
                entry_id=entry_id,
                changed_by=_api_changed_by(),
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        return jsonify(serialize_cash_entry(entry)), 201


@api_bp.get("/cash-books/<int:cash_book_id>/closings")
def list_cash_closings_via_api(cash_book_id: int):
    limit, offset = _paging()
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        closings, total = list_cash_closings(
            session=session,
            cash_book_id=cash_book_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        return jsonify(
            {
                "cash_book_id": cash_book_id,
                "total": total,
                "limit": limit,
                "offset": offset,
                "closings": [serialize_cash_closing(closing) for closing in closings],
            }
        )


@api_bp.post("/cash-books/<int:cash_book_id>/closings")
def create_cash_closing_via_api(cash_book_id: int):
    if not api_can_write():
        return forbidden()
    try:
        payload = _payload(CLOSING_FIELDS)
        closing_date = _optional_date(payload, "closing_date")
        difference_account_id = _optional_int(payload, "difference_account_id")
        description = _optional_text(payload, "description")
    except _PayloadError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        try:
            closing = create_cash_closing(
                session=session,
                cash_book_id=cash_book_id,
                closing_date=closing_date,
                counted_balance=payload.get("counted_balance"),
                difference_account_id=difference_account_id,
                description=description,
                changed_by=_api_changed_by(),
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        return jsonify(serialize_cash_closing(closing)), 201


@api_bp.get("/cash-books/<int:cash_book_id>/report")
def get_cash_report_via_api(cash_book_id: int):
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        try:
            report = cash_report(
                session=session,
                cash_book_id=cash_book_id,
                date_from=date_from,
                date_to=date_to,
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        return jsonify(report.as_dict())


@api_bp.get("/cash-books/<int:cash_book_id>/report.csv")
@linkable_download
def export_cash_report_csv_via_api(cash_book_id: int):
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        try:
            report = cash_report(
                session=session,
                cash_book_id=cash_book_id,
                date_from=date_from,
                date_to=date_to,
            )
        except CashBookError as exc:
            return validation_error(str(exc))
        content = cash_report_csv(report)
        file_name = cash_report_file_name(report)
    return Response(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@api_bp.get("/cash-books/<int:cash_book_id>/integrity")
def verify_cash_book_via_api(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        if _scoped_book(session, cash_book_id) is None:
            return _book_not_found()
        result = verify_cash_book_integrity(session=session, cash_book_id=cash_book_id)
        return jsonify(result.as_dict())
