"""Firmenstammdaten über die REST-API. Die IBAN bleibt beim Endpunkt ``bank-details``."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.company_master_data import (
    CompanyMasterDataError,
    serialize_company_master_data,
    update_company_master_data,
)


@api_bp.get("/companies/<int:company_id>/master-data")
def get_company_master_data_via_api(company_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        return jsonify(serialize_company_master_data(company))


@api_bp.patch("/companies/<int:company_id>/master-data")
def update_company_master_data_via_api(company_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            changed = update_company_master_data(
                session=session,
                company=company,
                changed_by=(current_api_user() or {}).get("username", "api"),
                **payload,
            )
        except CompanyMasterDataError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify({**serialize_company_master_data(company), "changed": changed})
