"""Kontenverwaltung: Kontenliste und Konto anlegen."""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.services.account_hierarchy import resolve_parent_account_id
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory, require_company_access
from domain.models import Account


@main_bp.get("/konten")
def accounts_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        accounts = (
            session.execute(
                scoped_select(Account, company_id=selected_company_id).order_by(Account.code)
            )
            .scalars()
            .all()
            if selected_company_id
            else []
        )

    return render_template(
        "konten.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
    )


@main_bp.post("/accounts")
def create_account():
    company_id = request.form.get("company_id", type=int)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    account_type = request.form.get("account_type", "").strip()

    if not company_id or not code or not name or not account_type:
        flash("Alle Felder für das Konto müssen ausgefüllt sein.", "error")
        return redirect(url_for("main.accounts_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)

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
            flash("Konto mit dieser Nummer existiert bereits.", "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.accounts_page", company_id=company_id))
