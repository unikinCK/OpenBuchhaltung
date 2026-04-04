from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from domain.models import Account, Company, Tenant

main_bp = Blueprint("main", __name__)


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


@main_bp.get("/")
def index():
    session_factory = _get_session_factory()
    with session_factory() as session:
        tenants = session.execute(select(Tenant).order_by(Tenant.name)).scalars().all()
        companies = session.execute(select(Company).order_by(Company.name)).scalars().all()
        accounts = session.execute(select(Account).order_by(Account.code)).scalars().all()
    return render_template(
        "index.html",
        tenants=tenants,
        companies=companies,
        accounts=accounts,
    )


@main_bp.post("/tenants")
def create_tenant_and_company():
    tenant_name = request.form.get("tenant_name", "").strip()
    company_name = request.form.get("company_name", "").strip()

    if not tenant_name or not company_name:
        flash("Mandant und Gesellschaft sind Pflichtfelder.", "error")
        return redirect(url_for("main.index"))

    session_factory = _get_session_factory()
    with session_factory() as session:
        tenant = Tenant(name=tenant_name)
        company = Company(name=company_name, currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Mandant oder Gesellschaft existiert bereits.", "error")
            return redirect(url_for("main.index"))

    flash("Mandant und Gesellschaft wurden angelegt.", "success")
    return redirect(url_for("main.index"))


@main_bp.post("/accounts")
def create_account():
    company_id = request.form.get("company_id", type=int)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    account_type = request.form.get("account_type", "").strip()

    if not company_id or not code or not name or not account_type:
        flash("Alle Felder für das Konto müssen ausgefüllt sein.", "error")
        return redirect(url_for("main.index"))

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            abort(404)

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
            flash("Konto mit dieser Nummer existiert bereits.", "error")
            return redirect(url_for("main.index"))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.index"))
