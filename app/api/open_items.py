"""Offene Posten über die API."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.journal_entries import parse_decimal
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    list_open_items,
    settle_open_item,
)
from domain.models import OpenItem


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _open_item_dict(item: OpenItem) -> dict[str, object]:
    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "company_id": item.company_id,
        "account_id": item.account_id,
        "account_code": item.account.code if item.account else None,
        "account_name": item.account.name if item.account else None,
        "journal_entry_id": item.journal_entry_id,
        "bank_transaction_id": item.bank_transaction_id,
        "item_type": item.item_type,
        "reference": item.reference,
        "counterparty": item.counterparty,
        "entry_date": item.entry_date.isoformat(),
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "original_amount": str(item.original_amount),
        "open_amount": str(item.open_amount),
        "currency_code": item.currency_code,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "settled_at": item.settled_at.isoformat() if item.settled_at else None,
        "settled_by": item.settled_by,
    }


@api_bp.get("/open-items")
def list_open_items_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    include_settled = request.args.get("include_settled", "").lower() in {"1", "true"}

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        items = list_open_items(
            session=session, company_id=company_id, include_settled=include_settled
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "open_items": [_open_item_dict(item) for item in items],
                }
            ),
            200,
        )


@api_bp.post("/open-items")
def create_open_item_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        account_id = int(payload.get("account_id"))
        entry_date = date.fromisoformat(payload.get("entry_date") or date.today().isoformat())
        due_date_raw = payload.get("due_date")
        amount = parse_decimal(str(payload.get("amount")))
        journal_entry_id = (
            int(payload["journal_entry_id"])
            if payload.get("journal_entry_id") is not None
            else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid payload format."}), 400

    due_date = None
    if due_date_raw:
        try:
            due_date = date.fromisoformat(due_date_raw)
        except ValueError:
            return jsonify({"error": "due_date must be an ISO date (YYYY-MM-DD)."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            item = create_open_item(
                session=session,
                payload=OpenItemInput(
                    company_id=company_id,
                    account_id=account_id,
                    journal_entry_id=journal_entry_id,
                    item_type=(payload.get("item_type") or "").strip(),
                    reference=(payload.get("reference") or "").strip(),
                    counterparty=(payload.get("counterparty") or "").strip() or None,
                    entry_date=entry_date,
                    due_date=due_date,
                    amount=amount,
                    changed_by=_api_changed_by(),
                ),
            )
        except OpenItemError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_open_item_dict(item)), 201


@api_bp.post("/open-items/<int:open_item_id>/settle")
def settle_open_item_via_api(open_item_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        amount = (
            parse_decimal(str(payload["amount"])) if payload.get("amount") is not None else None
        )
        bank_transaction_id = (
            int(payload["bank_transaction_id"])
            if payload.get("bank_transaction_id") is not None
            else None
        )
        journal_entry_id = (
            int(payload["journal_entry_id"])
            if payload.get("journal_entry_id") is not None
            else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid payload format."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        item = session.get(OpenItem, open_item_id)
        if item is None or api_scoped_company(session, item.company_id) is None:
            return jsonify({"error": "Open item not found."}), 404
        try:
            item = settle_open_item(
                session=session,
                open_item_id=open_item_id,
                amount=amount,
                bank_transaction_id=bank_transaction_id,
                journal_entry_id=journal_entry_id,
                changed_by=_api_changed_by(),
            )
        except OpenItemError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_open_item_dict(item)), 200
