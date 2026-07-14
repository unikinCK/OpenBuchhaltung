"""Kontenverwaltung: Kontenliste und Konto anlegen."""

from __future__ import annotations

from io import StringIO

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app.services.account_chart_import import (
    BUNDLED_ACCOUNT_CHART_FILES,
    import_account_chart_csv,
    import_bundled_account_chart,
)
from app.services.account_hierarchy import resolve_parent_account_id
from app.services.accounts import (
    AccountUpdateError,
    log_account_created,
    update_account_master_data,
)
from app.services.audit_log import list_audit_log_entries
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
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
        account_events = (
            list_audit_log_entries(
                session=session,
                company_id=selected_company_id,
                entity_type="account",
                limit=100,
            )
            if selected_company_id
            else []
        )

    return render_template(
        "konten.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        account_events=account_events,
        bundled_charts=sorted(BUNDLED_ACCOUNT_CHART_FILES),
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
            session.flush()
            log_account_created(
                session=session,
                account=account,
                changed_by=changed_by(),
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Konto mit dieser Nummer existiert bereits.", "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.accounts_page", company_id=company_id))


@main_bp.post("/accounts/<int:account_id>/update")
def update_account(account_id: int):
    name = request.form.get("name", "").strip()
    active_raw = request.form.get("is_active", "").strip().lower()
    if active_raw not in {"true", "false"}:
        flash("Ungültiger Kontostatus.", "error")
        return redirect(url_for("main.accounts_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        account = session.get(Account, account_id)
        if account is None:
            abort(404)
        require_company_access(session, account.company_id)
        try:
            changed = update_account_master_data(
                session=session,
                account=account,
                changed_by=changed_by(),
                name=name,
                is_active=active_raw == "true",
            )
        except AccountUpdateError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.accounts_page", company_id=account.company_id))
        session.commit()
        company_id = account.company_id

    flash(
        "Konto wurde geändert." if changed else "Keine Kontoänderung erkannt.",
        "success",
    )
    return redirect(url_for("main.accounts_page", company_id=company_id))


@main_bp.post("/accounts/import-chart")
def import_account_chart_action():
    company_id = request.form.get("company_id", type=int)
    chart = (request.form.get("chart") or "").strip().lower()
    uploaded_file = request.files.get("account_chart_csv")
    has_chart = bool(chart)
    has_upload = uploaded_file is not None and bool(uploaded_file.filename)

    if not company_id:
        flash("Bitte zuerst eine Gesellschaft auswählen.", "error")
        return redirect(url_for("main.accounts_page"))
    if has_chart == has_upload:
        flash("Bitte entweder SKR03/SKR04 wählen oder eine CSV-Datei hochladen.", "error")
        return redirect(url_for("main.accounts_page", company_id=company_id))
    if has_chart and chart not in BUNDLED_ACCOUNT_CHART_FILES:
        flash("Unbekannter Kontenrahmen.", "error")
        return redirect(url_for("main.accounts_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            if has_chart:
                report = import_bundled_account_chart(
                    session=session,
                    company_id=company.id,
                    chart=chart,
                    changed_by=changed_by(),
                )
            else:
                csv_text = uploaded_file.read().decode("utf-8-sig")
                report = import_account_chart_csv(
                    session=session,
                    company_id=company.id,
                    csv_stream=StringIO(csv_text),
                    changed_by=changed_by(),
                )
        except (UnicodeDecodeError, ValueError) as exc:
            flash(f"Kontenrahmen-Import fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))

    flash(
        f"Kontenrahmen-Import: {report.imported_rows} neu, "
        f"{report.duplicate_rows} Duplikate, {report.error_rows} Fehler.",
        "success" if report.error_rows == 0 else "error",
    )
    return redirect(url_for("main.accounts_page", company_id=company_id))
