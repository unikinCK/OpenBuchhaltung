"""Kontenverwaltung über die API."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.account_hierarchy import resolve_parent_account_id
from app.services.accounts import (
    AccountUpdateError,
    account_history,
    log_account_created,
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

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
            parent_account_id=resolve_parent_account_id(
                session=session, company_id=company.id, code=code
            ),
        )
        session.add(account)

        try:
            session.flush()
            log_account_created(
                session=session,
                account=account,
                changed_by=_api_changed_by(),
            )
            session.commit()
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
    unsupported_fields = set(payload) - {"name", "is_active"}
    if unsupported_fields:
        return (
            jsonify(
                {
                    "error": "Only name and is_active may be changed; "
                    "code and account_type are immutable."
                }
            ),
            400,
        )

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


@api_bp.get("/accounts")
def list_accounts():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        stmt = scoped_select(Account, company_id=company.id)
        if not include_inactive:
            stmt = stmt.where(Account.is_active.is_(True))
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
