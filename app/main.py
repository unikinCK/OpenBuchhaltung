from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.reports import trial_balance_for_company
from app.services.scoping import scoped_select
from domain.models import Account, Company, Tenant
from domain.services.journal_entry_validation import JournalEntryValidationError

main_bp = Blueprint("main", __name__)


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


@main_bp.get("/")
def index():
    selected_company_id = request.args.get("company_id", type=int)

    session_factory = _get_session_factory()
    with session_factory() as session:
        tenants = session.execute(scoped_select(Tenant).order_by(Tenant.name)).scalars().all()
        companies = session.execute(scoped_select(Company).order_by(Company.name)).scalars().all()

        if selected_company_id is None and companies:
            selected_company_id = companies[0].id

        account_query = scoped_select(
            Account,
            company_id=selected_company_id,
        ).order_by(Account.code)
        accounts = session.execute(account_query).scalars().all() if selected_company_id else []

        trial_balance = (
            trial_balance_for_company(session=session, company_id=selected_company_id)
            if selected_company_id
            else []
        )

    return render_template(
        "index.html",
        tenants=tenants,
        companies=companies,
        accounts=accounts,
        selected_company_id=selected_company_id,
        trial_balance=trial_balance,
        today=date.today().isoformat(),
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
            return redirect(url_for("main.index", company_id=company_id))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.index", company_id=company_id))


@main_bp.post("/journal-entries")
def create_journal_entry_from_form():
    company_id = request.form.get("company_id", type=int)
    entry_date_raw = request.form.get("entry_date", "").strip()
    description = request.form.get("description", "").strip()
    debit_account_id = request.form.get("debit_account_id", type=int)
    credit_account_id = request.form.get("credit_account_id", type=int)
    amount_raw = request.form.get("amount", "").strip()

    if not company_id or not entry_date_raw or not description:
        flash("Gesellschaft, Datum und Beschreibung sind Pflichtfelder.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    if not debit_account_id or not credit_account_id:
        flash("Bitte Soll- und Habenkonto auswählen.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    try:
        parsed_date = date.fromisoformat(entry_date_raw)
    except ValueError:
        flash("Ungültiges Datum.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    try:
        amount = parse_decimal(amount_raw)
        entry_payload = JournalEntryInput(
            company_id=company_id,
            entry_date=parsed_date,
            description=description,
            status="posted",
            changed_by="web-form",
            lines=[
                JournalLineInput(
                    account_id=debit_account_id,
                    debit_amount=amount,
                    credit_amount=parse_decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=credit_account_id,
                    debit_amount=parse_decimal("0.00"),
                    credit_amount=amount,
                ),
            ],
        )

        session_factory = _get_session_factory()
        with session_factory() as session:
            entry = create_journal_entry(session=session, payload=entry_payload)

    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index", company_id=company_id))

    flash(f"Buchung {entry.posting_number} wurde gespeichert.", "success")
    return redirect(url_for("main.index", company_id=company_id))
