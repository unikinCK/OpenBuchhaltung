"""Kontenverwaltung über die API."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

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
from app.services.account_ledger import (
    DEFAULT_LEDGER_LIMIT,
    AccountLedgerError,
    account_ledger,
    serialize_account_ledger,
)
from app.services.accounts import (
    AccountUpdateError,
    account_history,
    create_account_with_audit,
    serialize_account,
    update_account_master_data,
)
from app.services.scoping import scoped_select
from domain.models import Account


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


@api_bp.post("/accounts")
def create_account():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    account_type = (payload.get("account_type") or "").strip()

    if not company_id or not code or not name or not account_type:
        return jsonify({"error": "company_id, code, name and account_type are required."}), 400
    subledger = payload.get("subledger")
    if subledger is not None and not isinstance(subledger, str):
        return jsonify({"error": "subledger must be debtor, creditor or null."}), 400

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
                account_type=account_type,
                subledger=subledger,
                changed_by=_api_changed_by(),
            )
            session.commit()
        except AccountUpdateError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 400
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Account code already exists for this company."}), 409

        return (
            jsonify(serialize_account(account)),
            201,
        )


@api_bp.patch("/accounts/<int:account_id>")
def update_account(account_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    unsupported_fields = set(payload) - {"name", "is_active", "subledger", "account_type"}
    if unsupported_fields:
        return (
            jsonify(
                {
                    "error": "Only name, is_active and subledger may be changed; "
                    "code is immutable and account_type may only repair invalid values."
                }
            ),
            400,
        )
    extra_changes = {
        field: payload[field] for field in ("subledger", "account_type") if field in payload
    }

    session_factory = get_session_factory()
    with session_factory() as session:
        account = session.get(Account, account_id)
        if account is None or api_scoped_company(session, account.company_id) is None:
            return jsonify({"error": "Account not found."}), 404
        try:
            changed = update_account_master_data(
                session=session,
                account=account,
                changed_by=_api_changed_by(),
                name=payload.get("name") if "name" in payload else None,
                is_active=payload.get("is_active") if "is_active" in payload else None,
                **extra_changes,
            )
        except AccountUpdateError as exc:
            return jsonify({"error": str(exc)}), 400
        session.commit()
        response = serialize_account(account)
        response["changed"] = changed
        return jsonify(response), 200


@api_bp.get("/accounts/<int:account_id>/history")
def get_account_history(account_id: int):
    limit = request.args.get("limit", default=100, type=int)
    if limit is None:
        return jsonify({"error": "limit must be an integer."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        account = session.get(Account, account_id)
        if account is None or api_scoped_company(session, account.company_id) is None:
            return jsonify({"error": "Account not found."}), 404
        entries = account_history(session=session, account_id=account.id, limit=limit)
        return (
            jsonify(
                {
                    "account": serialize_account(account),
                    "entries": entries,
                    "limit": max(1, min(limit, 500)),
                }
            ),
            200,
        )


@api_bp.get("/accounts/<int:account_id>/ledger")
def get_account_ledger(account_id: int):
    """Kontoblatt: Buchungszeilen des Kontos mit Anfangs- und laufendem Saldo."""
    return _account_ledger_response(account_id=account_id)


@api_bp.get("/accounts/ledger")
def get_account_ledger_by_query():
    """Kontoblatt per ``account_id`` oder ``company_id`` + ``account_code``."""
    account_id = request.args.get("account_id", type=int)
    account_code = (request.args.get("account_code") or "").strip() or None
    company_id = request.args.get("company_id", type=int)
    if account_id is None and account_code is None:
        return jsonify({"error": "account_id or account_code is required."}), 400
    if account_id is None and not company_id:
        return jsonify({"error": "company_id is required with account_code."}), 400
    return _account_ledger_response(
        account_id=account_id, company_id=company_id, account_code=account_code
    )


def _account_ledger_response(
    *,
    account_id: int | None,
    company_id: int | None = None,
    account_code: str | None = None,
):
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400
    fiscal_year_id = request.args.get("fiscal_year_id", type=int)
    limit = request.args.get("limit", default=DEFAULT_LEDGER_LIMIT, type=int)
    offset = request.args.get("offset", default=0, type=int)
    if limit is None or offset is None:
        return jsonify({"error": "limit and offset must be integers."}), 400
    sort = (request.args.get("sort") or "").strip() or None
    direction = (request.args.get("dir") or "asc").strip().lower()
    if direction not in {"asc", "desc"}:
        return jsonify({"error": "dir must be asc or desc."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if account_id is not None:
            account = session.get(Account, account_id)
        else:
            account = session.execute(
                scoped_select(Account, company_id=company_id).where(
                    Account.code == account_code
                )
            ).scalar_one_or_none()
        if account is None or api_scoped_company(session, account.company_id) is None:
            return jsonify({"error": "Account not found."}), 404
        if company_id and account.company_id != company_id:
            return jsonify({"error": "Account not found."}), 404
        try:
            ledger = account_ledger(
                session=session,
                account=account,
                date_from=date_from,
                date_to=date_to,
                fiscal_year_id=fiscal_year_id,
                limit=limit,
                offset=offset,
                sort=sort,
                descending=direction == "desc",
            )
        except AccountLedgerError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(serialize_account_ledger(ledger)), 200


@api_bp.get("/accounts")
def list_accounts():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}
    code = (request.args.get("code") or "").strip() or None
    query = (request.args.get("q") or "").strip() or None
    account_type = (request.args.get("account_type") or "").strip() or None

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = scoped_select(Account, company_id=company.id)
        if not include_inactive:
            stmt = stmt.where(Account.is_active.is_(True))
        if code:
            stmt = stmt.where(Account.code == code)
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(Account.code.ilike(pattern) | Account.name.ilike(pattern))
        if account_type:
            stmt = stmt.where(Account.account_type == account_type)
        accounts = session.execute(stmt.order_by(Account.code)).scalars().all()

    return (
        jsonify(
            {
                "company_id": company_id,
                "accounts": [
                    serialize_account(account)
                    for account in accounts
                ],
            }
        ),
        200,
    )
