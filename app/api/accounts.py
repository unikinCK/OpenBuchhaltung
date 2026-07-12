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
from app.services.account_hierarchy import resolve_parent_account_id
from app.services.scoping import scoped_select
from domain.models import Account


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
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Account code already exists for this company."}), 409

        return (
            jsonify(
                {
                    "id": account.id,
                    "tenant_id": account.tenant_id,
                    "company_id": account.company_id,
                    "code": account.code,
                    "name": account.name,
                    "account_type": account.account_type,
                }
            ),
            201,
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
                    {
                        "id": account.id,
                        "code": account.code,
                        "name": account.name,
                        "account_type": account.account_type,
                        "is_active": account.is_active,
                    }
                    for account in accounts
                ],
            }
        ),
        200,
    )
