"""Open-Banking-Zugänge (PSD2 über Enable Banking) auf der Bankseite.

Ablauf im Browser: Zugang anlegen → „Bank-Zustimmung erteilen“ leitet zur
Bank weiter → Rücksprung auf ``/bank/open-banking/callback`` → „Umsätze
abrufen“, solange die Zustimmung gilt.
"""

from __future__ import annotations

from datetime import date

from flask import abort, current_app, flash, redirect, render_template, request, url_for

from app.auth import WRITE_ROLES, current_user
from app.services.open_banking import (
    OpenBankingError,
    complete_open_banking_authorization,
    create_open_banking_connection,
    find_connection_by_state,
    list_open_banking_institutes,
    open_banking_config,
    set_open_banking_connection_active,
    start_open_banking_authorization,
    sync_open_banking_transactions,
)
from app.web.blueprint import main_bp
from app.web.formatting import format_date
from app.web.helpers import changed_by, get_session_factory, require_company_access
from domain.models import OpenBankingConnection


def _config():
    return open_banking_config(
        current_app.config,
        fallback_redirect_url=url_for("main.open_banking_callback", _external=True),
    )


def _bank_page(company_id: int | None):
    return redirect(url_for("main.bank_page", company_id=company_id))


def _form_date(field: str) -> date | None:
    raw = (request.form.get(field) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise OpenBankingError(f"Ungültiges Datum im Feld {field}.") from exc


def _load_connection(session, connection_id: int) -> OpenBankingConnection:
    connection = session.get(OpenBankingConnection, connection_id)
    if connection is None:
        abort(404)
    require_company_access(session, connection.company_id)
    return connection


@main_bp.get("/bank/open-banking/banken")
def open_banking_institutes_page():
    """Bankenliste von Enable Banking durchsuchen (Name für den Zugang)."""
    country = (request.args.get("country") or "DE").strip().upper()
    query = (request.args.get("q") or "").strip()
    company_id = request.args.get("company_id", type=int)
    institutes: list[dict] = []
    error = None
    if query or request.args.get("alle"):
        try:
            institutes = list_open_banking_institutes(
                config=_config(), country=country, query=query
            )
        except OpenBankingError as exc:
            error = str(exc)
    return render_template(
        "open_banking_institutes.html",
        country=country,
        q=query,
        company_id=company_id,
        institutes=institutes,
        error=error,
    )


@main_bp.post("/bank/open-banking/anlegen")
def open_banking_connection_create_action():
    company_id = request.form.get("company_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    if not company_id or not bank_account_id:
        flash("Gesellschaft und Bankkonto sind Pflichtfelder.", "error")
        return _bank_page(company_id)

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            connection = create_open_banking_connection(
                session=session,
                company_id=company_id,
                bank_account_id=bank_account_id,
                name=request.form.get("name") or "",
                aspsp_name=request.form.get("aspsp_name") or "",
                aspsp_country=request.form.get("aspsp_country") or "DE",
                psu_type=request.form.get("psu_type") or "business",
                iban=request.form.get("iban"),
                changed_by=changed_by(),
            )
        except OpenBankingError as exc:
            flash(str(exc), "error")
            return _bank_page(company_id)

    flash(
        f"Open-Banking-Zugang „{connection.name}“ wurde angelegt — jetzt die "
        "Bank-Zustimmung erteilen.",
        "success",
    )
    return _bank_page(company_id)


@main_bp.post("/bank/open-banking/<int:connection_id>/autorisieren")
def open_banking_authorize_action(connection_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        connection = _load_connection(session, connection_id)
        company_id = connection.company_id
        try:
            start = start_open_banking_authorization(
                session=session,
                connection_id=connection_id,
                config=_config(),
                changed_by=changed_by(),
            )
        except OpenBankingError as exc:
            flash(str(exc), "error")
            return _bank_page(company_id)
    return redirect(start.url)


@main_bp.get("/bank/open-banking/callback")
def open_banking_callback():
    """Rücksprung von der Bank (``code``/``state`` bzw. ``error``)."""
    state = request.args.get("state") or ""
    user = current_user()
    if user is None or user["role"] not in WRITE_ROLES:
        flash("Ihre Rolle erlaubt nur Lesezugriff.", "error")
        return redirect(url_for("main.index"))

    session_factory = get_session_factory()
    with session_factory() as session:
        connection = find_connection_by_state(session, state)
        if connection is None:
            flash("Autorisierung nicht gefunden oder bereits abgeschlossen.", "error")
            return redirect(url_for("main.bank_page"))
        require_company_access(session, connection.company_id)
        company_id = connection.company_id

        error = request.args.get("error")
        if error:
            connection.auth_state = None
            connection.auth_started_at = None
            session.commit()
            description = request.args.get("error_description") or error
            flash(f"Die Bank hat die Zustimmung nicht erteilt: {description[:200]}", "error")
            return _bank_page(company_id)

        try:
            connection = complete_open_banking_authorization(
                session=session,
                state=state,
                code=request.args.get("code") or "",
                config=_config(),
                secret_key=current_app.config.get("SECRET_KEY"),
                changed_by=changed_by(),
            )
        except OpenBankingError as exc:
            flash(str(exc), "error")
            return _bank_page(company_id)
        valid_until = connection.consent_valid_until
        suffix = f" (gültig bis {format_date(valid_until)})" if valid_until else ""
        flash(f"Bank-Zustimmung für „{connection.name}“ erteilt{suffix}.", "success")
    return _bank_page(company_id)


@main_bp.post("/bank/open-banking/<int:connection_id>/abrufen")
def open_banking_sync_action(connection_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        connection = _load_connection(session, connection_id)
        company_id = connection.company_id
        try:
            report = sync_open_banking_transactions(
                session=session,
                connection_id=connection_id,
                config=_config(),
                secret_key=current_app.config.get("SECRET_KEY"),
                from_date=_form_date("from_date"),
                to_date=_form_date("to_date"),
                changed_by=changed_by(),
            )
        except OpenBankingError as exc:
            flash(str(exc), "error")
            return _bank_page(company_id)

    flash(
        f"Open-Banking-Abruf: {report.imported_rows} neu, {report.duplicate_rows} Duplikate, "
        f"{report.error_rows} Fehler.",
        "success" if report.error_rows == 0 else "error",
    )
    return _bank_page(company_id)


@main_bp.post("/bank/open-banking/<int:connection_id>/deaktivieren")
def open_banking_deactivate_action(connection_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        connection = _load_connection(session, connection_id)
        company_id = connection.company_id
        try:
            config = _config()
        except OpenBankingError:
            config = None  # ohne Konfiguration nur lokal deaktivieren
        set_open_banking_connection_active(
            session=session,
            connection_id=connection_id,
            is_active=False,
            changed_by=changed_by(),
            config=config,
            secret_key=current_app.config.get("SECRET_KEY"),
        )

    flash("Open-Banking-Zugang wurde deaktiviert und die Bank-Sitzung widerrufen.", "success")
    return _bank_page(company_id)
