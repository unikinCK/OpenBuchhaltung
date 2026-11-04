"""Offene Posten über die API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.dunning import (
    DunningError,
    dunning_proposals,
    record_dunning,
    serialize_proposal,
)
from app.services.journal_entries import parse_decimal
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    is_overdue,
    list_open_items,
    open_item_matches_query,
    open_item_partner_number,
    set_open_item_partner,
    settle_open_item,
)
from app.services.partners import find_partner_by_number
from domain.models import OpenItem


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


class _PartnerArgumentError(ValueError):
    """``partner_id``/``partner_number`` lassen sich nicht auflösen."""


def _resolve_partner_id(session, company_id: int, payload: dict) -> int | None:
    """Partner aus ``partner_id`` oder ``partner_number`` (Debitoren-/Kreditorennummer)."""
    raw_number = payload.get("partner_number")
    if raw_number not in (None, ""):
        partner = find_partner_by_number(
            session=session, company_id=company_id, number=str(raw_number)
        )
        if partner is None:
            raise _PartnerArgumentError(f"Partnernummer {raw_number} nicht gefunden.")
        return partner.id
    raw_id = payload.get("partner_id")
    if raw_id in (None, ""):
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError) as exc:
        raise _PartnerArgumentError("partner_id must be an integer.") from exc


def _optional_decimal(value: object) -> Decimal | None:
    return parse_decimal(str(value)) if value not in (None, "") else None


def _optional_rate(value: object) -> Decimal | None:
    return parse_decimal(str(value), places=None) if value not in (None, "") else None


def _open_item_dict(item: OpenItem) -> dict[str, object]:
    return {
        "id": item.id,
        "tenant_id": item.tenant_id,
        "company_id": item.company_id,
        "account_id": item.account_id,
        "account_code": item.account.code if item.account else None,
        "account_name": item.account.name if item.account else None,
        "journal_entry_id": item.journal_entry_id,
        "settlement_journal_entry_id": item.settlement_journal_entry_id,
        "settlement_amount": (
            str(item.settlement_amount) if item.settlement_amount is not None else None
        ),
        "bank_transaction_id": item.bank_transaction_id,
        "item_type": item.item_type,
        "reference": item.reference,
        "partner_id": item.partner_id,
        "partner_number": open_item_partner_number(item),
        "partner_name": item.partner.name if item.partner else None,
        "counterparty": item.counterparty,
        "counterparty_iban": item.counterparty_iban,
        "counterparty_bic": item.counterparty_bic,
        "entry_date": item.entry_date.isoformat(),
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "original_amount": str(item.original_amount),
        "open_amount": str(item.open_amount),
        "currency_code": item.currency_code,
        "foreign_currency_code": item.foreign_currency_code,
        "foreign_original_amount": (
            str(item.foreign_original_amount) if item.foreign_original_amount is not None else None
        ),
        "foreign_open_amount": (
            str(item.foreign_open_amount) if item.foreign_open_amount is not None else None
        ),
        "exchange_rate": str(item.exchange_rate) if item.exchange_rate is not None else None,
        "status": item.status,
        "dunning_level": item.dunning_level,
        "last_dunning_date": (
            item.last_dunning_date.isoformat() if item.last_dunning_date else None
        ),
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
    # Überfällig: offen und Fälligkeit vor heute (wie die Dashboard-Kachel).
    overdue = request.args.get("overdue", "").lower() in {"1", "true"}
    query = (request.args.get("q") or "").strip() or None
    partner_id = request.args.get("partner_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        items = list_open_items(
            session=session,
            company_id=company_id,
            include_settled=include_settled,
            partner_id=partner_id,
        )
        if overdue:
            items = [item for item in items if is_overdue(item)]
        if query:
            items = [item for item in items if open_item_matches_query(item, query)]
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
        foreign_currency_code = str(payload.get("foreign_currency_code") or "").strip() or None
        foreign_amount = _optional_decimal(payload.get("foreign_amount"))
        exchange_rate = _optional_rate(payload.get("exchange_rate"))
        # Mit Fremdbetrag ergibt sich der Betrag in Firmenwährung aus dem Kurs.
        amount = (
            parse_decimal(str(payload.get("amount")))
            if payload.get("amount") not in (None, "") or foreign_amount is None
            else Decimal("0.00")
        )
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
            partner_id = _resolve_partner_id(session, company_id, payload)
        except _PartnerArgumentError as exc:
            return jsonify({"error": str(exc)}), 422
        try:
            item = create_open_item(
                session=session,
                payload=OpenItemInput(
                    company_id=company_id,
                    account_id=account_id,
                    journal_entry_id=journal_entry_id,
                    partner_id=partner_id,
                    item_type=(payload.get("item_type") or "").strip(),
                    reference=(payload.get("reference") or "").strip(),
                    counterparty=(payload.get("counterparty") or "").strip() or None,
                    counterparty_iban=(payload.get("counterparty_iban") or "").strip()
                    or None,
                    counterparty_bic=(payload.get("counterparty_bic") or "").strip() or None,
                    entry_date=entry_date,
                    due_date=due_date,
                    amount=amount,
                    changed_by=_api_changed_by(),
                    foreign_currency_code=foreign_currency_code,
                    foreign_amount=foreign_amount,
                    exchange_rate=exchange_rate,
                ),
            )
        except OpenItemError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_open_item_dict(item)), 201


@api_bp.post("/open-items/<int:open_item_id>/partner")
def set_open_item_partner_via_api(open_item_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        item = session.get(OpenItem, open_item_id)
        if item is None or api_scoped_company(session, item.company_id) is None:
            return jsonify({"error": "Open item not found."}), 404
        try:
            partner_id = _resolve_partner_id(session, item.company_id, payload)
            item = set_open_item_partner(
                session=session,
                open_item_id=open_item_id,
                partner_id=partner_id,
                changed_by=_api_changed_by(),
            )
        except (_PartnerArgumentError, OpenItemError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_open_item_dict(item)), 200


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
        foreign_amount = _optional_decimal(payload.get("foreign_amount"))
        exchange_rate = _optional_rate(payload.get("exchange_rate"))
        settlement_date = (
            date.fromisoformat(str(payload["settlement_date"]))
            if payload.get("settlement_date")
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
                foreign_amount=foreign_amount,
                exchange_rate=exchange_rate,
                settlement_date=settlement_date,
            )
        except OpenItemError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_open_item_dict(item)), 200


@api_bp.get("/dunning-proposals")
def list_dunning_proposals_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        proposals = dunning_proposals(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "proposals": [serialize_proposal(p) for p in proposals],
                }
            ),
            200,
        )


@api_bp.post("/open-items/<int:open_item_id>/dunning")
def record_dunning_via_api(open_item_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    level = payload.get("level")
    if level is not None:
        try:
            level = int(level)
        except (TypeError, ValueError):
            return jsonify({"error": "level must be an integer."}), 400
    dunning_date = None
    if payload.get("dunning_date"):
        try:
            dunning_date = date.fromisoformat(str(payload["dunning_date"]))
        except ValueError:
            return jsonify({"error": "dunning_date must be an ISO date."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        item = session.get(OpenItem, open_item_id)
        if item is None or api_scoped_company(session, item.company_id) is None:
            return jsonify({"error": "Open item not found."}), 404
        try:
            item = record_dunning(
                session=session,
                open_item_id=open_item_id,
                changed_by=_api_changed_by(),
                level=level,
                dunning_date=dunning_date,
            )
        except DunningError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_open_item_dict(item)), 200
