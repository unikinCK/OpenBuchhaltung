from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.services.mcp_client import MCPError, call_mcp_server
from domain.models import Account, Company, Tenant

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


@api_bp.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@api_bp.post("/tenants")
def create_tenant_with_company():
    payload = request.get_json(silent=True) or {}
    tenant_name = (payload.get("tenant_name") or "").strip()
    company_name = (payload.get("company_name") or "").strip()
    currency_code = (payload.get("currency_code") or "EUR").strip()

    if not tenant_name or not company_name:
        return jsonify({"error": "tenant_name and company_name are required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        tenant = Tenant(name=tenant_name)
        company = Company(name=company_name, currency_code=currency_code, tenant=tenant)
        session.add_all([tenant, company])

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Tenant or company already exists."}), 409

        return (
            jsonify(
                {
                    "tenant": {"id": tenant.id, "name": tenant.name},
                    "company": {
                        "id": company.id,
                        "name": company.name,
                        "tenant_id": company.tenant_id,
                        "currency_code": company.currency_code,
                    },
                }
            ),
            201,
        )


@api_bp.get("/companies")
def list_companies():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies = session.execute(select(Company).order_by(Company.name)).scalars().all()

    return (
        jsonify(
            [
                {
                    "id": company.id,
                    "tenant_id": company.tenant_id,
                    "name": company.name,
                    "currency_code": company.currency_code,
                }
                for company in companies
            ]
        ),
        200,
    )


@api_bp.post("/accounts")
def create_account():
    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    account_type = (payload.get("account_type") or "").strip()

    if not company_id or not code or not name or not account_type:
        return jsonify({"error": "company_id, code, name and account_type are required."}), 400

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
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


@api_bp.post("/mcp/call")
def mcp_call():
    payload = request.get_json(silent=True) or {}
    method = (payload.get("method") or "").strip()
    params = payload.get("params") or {}
    request_id = payload.get("id", "openbuchhaltung-mcp-call")

    if not method:
        return jsonify({"error": "method is required."}), 400

    if not isinstance(params, dict):
        return jsonify({"error": "params must be an object."}), 400

    server_url = current_app.config.get("MCP_SERVER_URL")
    try:
        mcp_response = call_mcp_server(
            server_url=server_url,
            method=method,
            params=params,
            request_id=request_id,
        )
    except MCPError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(mcp_response), 200
