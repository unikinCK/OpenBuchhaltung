"""Mandanten und Gesellschaften."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_create_tenant,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_tenant_id
from app.services.scoping import scoped_select
from domain.models import Company, Tenant


@api_bp.post("/tenants")
def create_tenant_with_company():
    if not api_can_create_tenant():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    tenant_name = (payload.get("tenant_name") or "").strip()
    company_name = (payload.get("company_name") or "").strip()
    currency_code = (payload.get("currency_code") or "EUR").strip()

    if not tenant_name or not company_name:
        return jsonify({"error": "tenant_name and company_name are required."}), 400

    session_factory = get_session_factory()
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
    session_factory = get_session_factory()
    with session_factory() as session:
        companies = (
            session.execute(
                scoped_select(Company, tenant_id=current_api_tenant_id()).order_by(Company.name)
            )
            .scalars()
            .all()
        )

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
