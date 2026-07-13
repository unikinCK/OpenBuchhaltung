"""Audit-Log-Ansicht."""

from __future__ import annotations

from flask import render_template, request

from app.auth import current_tenant_id
from app.services.audit_log import list_audit_log_entries, verify_audit_log_integrity
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory


@main_bp.get("/audit-log")
def audit_log_page():
    tenant_scope = current_tenant_id()
    company_filter_id = request.args.get("company_id", type=int)
    entity_type = (request.args.get("entity_type") or "").strip() or None
    action = (request.args.get("action") or "").strip() or None
    limit = request.args.get("limit", default=100, type=int) or 100
    verify_integrity = request.args.get("verify_integrity") == "1"

    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        accessible_company_ids = {company.id for company in companies}
        if company_filter_id is not None and company_filter_id not in accessible_company_ids:
            company_filter_id = None

        entries = list_audit_log_entries(
            session=session,
            tenant_id=tenant_scope,
            company_id=company_filter_id,
            entity_type=entity_type,
            action=action,
            limit=limit,
        )
        integrity_result = (
            verify_audit_log_integrity(session=session, tenant_id=tenant_scope)
            if verify_integrity
            else None
        )

    return render_template(
        "audit_log.html",
        companies=companies,
        selected_company_id=selected_company_id,
        entries=entries,
        company_filter_id=company_filter_id,
        entity_type=entity_type or "",
        action=action or "",
        limit=max(1, min(limit, 500)),
        integrity_result=integrity_result,
    )
