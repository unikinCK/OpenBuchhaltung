"""Verwaltung: Mandanten- und Gesellschaftsanlage."""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.auth import current_tenant_id
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory
from domain.models import Account, Company, Tenant


@main_bp.get("/verwaltung")
def admin_page():
    tenant_scope = current_tenant_id()
    session_factory = get_session_factory()
    with session_factory() as session:
        tenant_query = scoped_select(Tenant).order_by(Tenant.name)
        if tenant_scope is not None:
            tenant_query = tenant_query.where(Tenant.id == tenant_scope)
        tenants = session.execute(tenant_query).scalars().all()
        companies, selected_company_id = company_context(session)
        account_count = (
            len(
                session.execute(scoped_select(Account, company_id=selected_company_id))
                .scalars()
                .all()
            )
            if selected_company_id
            else 0
        )

    return render_template(
        "verwaltung.html",
        tenants=tenants,
        companies=companies,
        selected_company_id=selected_company_id,
        account_count=account_count,
        is_global_admin=tenant_scope is None,
    )


@main_bp.post("/tenants")
def create_tenant_and_company():
    if current_tenant_id() is not None:
        flash("Neue Mandanten kann nur ein globaler Administrator anlegen.", "error")
        return redirect(url_for("main.admin_page"))

    tenant_name = request.form.get("tenant_name", "").strip()
    company_name = request.form.get("company_name", "").strip()

    if not tenant_name or not company_name:
        flash("Mandant und Gesellschaft sind Pflichtfelder.", "error")
        return redirect(url_for("main.admin_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        tenant = Tenant(name=tenant_name)
        company = Company(name=company_name, currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Mandant oder Gesellschaft existiert bereits.", "error")
            return redirect(url_for("main.admin_page"))

    flash("Mandant und Gesellschaft wurden angelegt.", "success")
    return redirect(url_for("main.admin_page"))
