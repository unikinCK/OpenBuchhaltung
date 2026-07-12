"""Audit-Log über die API."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_scoped_company, get_session_factory
from app.auth import current_api_tenant_id
from app.services.audit_log import list_audit_log_entries, serialize_audit_log_entry


@api_bp.get("/audit-log")
def list_audit_log_via_api():
    tenant_scope = current_api_tenant_id()
    company_id = request.args.get("company_id", type=int)
    requested_tenant_id = request.args.get("tenant_id", type=int)
    entity_type = (request.args.get("entity_type") or "").strip() or None
    action = (request.args.get("action") or "").strip() or None
    limit = request.args.get("limit", default=100, type=int)

    if limit is None:
        return jsonify({"error": "limit must be an integer."}), 400
    if (
        tenant_scope is not None
        and requested_tenant_id is not None
        and requested_tenant_id != tenant_scope
    ):
        return jsonify({"error": "Forbidden."}), 403

    session_factory = get_session_factory()
    with session_factory() as session:
        if company_id is not None and api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404

        entries = list_audit_log_entries(
            session=session,
            tenant_id=tenant_scope if tenant_scope is not None else requested_tenant_id,
            company_id=company_id,
            entity_type=entity_type,
            action=action,
            limit=limit,
        )
        return (
            jsonify(
                {
                    "entries": [serialize_audit_log_entry(entry) for entry in entries],
                    "limit": max(1, min(limit, 500)),
                }
            ),
            200,
        )
