"""Täglicher automatischer Bankabruf – Schalter auf der Bankseite und der Seite „Bank-MCP-Abruf“.

Der Abruf selbst läuft im Compose-Dienst ``bank-sync`` (siehe
:mod:`app.services.bank_auto_sync`); hier wird er je Zugang ein- und ausgeschaltet oder
sofort ausgeführt.
"""

from __future__ import annotations

from flask import abort, current_app, flash, redirect, request, url_for

from app.auth import current_user
from app.services.bank_auto_sync import (
    STATUS_OK,
    BankAutoSyncError,
    disable_bank_auto_sync,
    enable_fints_auto_sync,
    enable_mcp_auto_sync,
    enable_open_banking_auto_sync,
    run_bank_auto_syncs,
)
from app.services.mcp_bank_sync import McpBankSyncError, ServerCaller
from app.web.blueprint import main_bp
from app.web.helpers import changed_by, get_session_factory, require_company_access
from domain.models import BankAutoSync, FinTSConnection, OpenBankingConnection


def _user_id() -> int | None:
    return (current_user() or {}).get("id")


def _bank_page(company_id: int | None):
    return redirect(url_for("main.bank_page", company_id=company_id, _anchor="online-abruf"))


@main_bp.post("/bank/fints/<int:connection_id>/automatik")
def fints_auto_sync_enable_action(connection_id: int):
    with get_session_factory()() as session:
        connection = session.get(FinTSConnection, connection_id)
        if connection is None:
            abort(404)
        require_company_access(session, connection.company_id)
        company_id = connection.company_id
        try:
            enable_fints_auto_sync(
                session,
                connection_id=connection.id,
                pin=request.form.get("pin") or "",
                secret_key=current_app.config.get("SECRET_KEY"),
                user_id=_user_id(),
                changed_by=changed_by(),
            )
        except BankAutoSyncError as exc:
            flash(str(exc), "error")
            return _bank_page(company_id)
    flash(
        "Täglicher automatischer Abruf ist eingeschaltet; die PIN liegt dafür verschlüsselt vor.",
        "success",
    )
    return _bank_page(company_id)


@main_bp.post("/bank/open-banking/<int:connection_id>/automatik")
def open_banking_auto_sync_enable_action(connection_id: int):
    with get_session_factory()() as session:
        connection = session.get(OpenBankingConnection, connection_id)
        if connection is None:
            abort(404)
        require_company_access(session, connection.company_id)
        company_id = connection.company_id
        try:
            enable_open_banking_auto_sync(
                session, connection_id=connection.id, user_id=_user_id(), changed_by=changed_by()
            )
        except BankAutoSyncError as exc:
            flash(str(exc), "error")
            return _bank_page(company_id)
    flash("Täglicher automatischer Abruf ist eingeschaltet.", "success")
    return _bank_page(company_id)


@main_bp.post("/bank/mcp-abruf/automatik")
def mcp_auto_sync_enable_action():
    company_id = request.form.get("company_id", type=int)
    server_id = request.form.get("server_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    wallet_id = (request.form.get("wallet_id") or "").strip()
    if not company_id or not server_id or not bank_account_id or not wallet_id:
        flash("Gesellschaft, MCP-Server, Wallet und Bankkonto sind Pflichtfelder.", "error")
        return _bank_page(company_id)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            with ServerCaller(
                session_factory=session_factory,
                server_id=server_id,
                tenant_id=company.tenant_id,
                company_id=company.id,
                user=current_user(),
            ) as caller:
                enable_mcp_auto_sync(
                    session,
                    company=company,
                    caller=caller,
                    server_id=server_id,
                    bank_account_id=bank_account_id,
                    wallet_id=wallet_id,
                    user_id=_user_id(),
                    changed_by=changed_by(),
                )
        except (BankAutoSyncError, McpBankSyncError) as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("main.mcp_bank_sync_page", company_id=company_id, server_id=server_id)
            )
    flash("Täglicher automatischer Abruf der Wallet ist eingeschaltet.", "success")
    return _bank_page(company_id)


@main_bp.post("/bank/automatik/<int:auto_sync_id>/ausschalten")
def bank_auto_sync_disable_action(auto_sync_id: int):
    with get_session_factory()() as session:
        entry = session.get(BankAutoSync, auto_sync_id)
        if entry is None:
            abort(404)
        require_company_access(session, entry.company_id)
        company_id = entry.company_id
        disable_bank_auto_sync(session, auto_sync_id=entry.id, changed_by=changed_by())
    flash("Automatischer Abruf ist ausgeschaltet.", "success")
    return _bank_page(company_id)


@main_bp.post("/bank/automatik/abrufen")
def bank_auto_sync_run_action():
    company_id = request.form.get("company_id", type=int)
    auto_sync_id = request.form.get("auto_sync_id", type=int)
    if not company_id:
        abort(400)
    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
    runs = run_bank_auto_syncs(
        session_factory,
        config=current_app.config,
        company_id=company_id,
        auto_sync_id=auto_sync_id,
    )
    if not runs:
        flash("Kein eingeschalteter, laufbereiter automatischer Abruf.", "info")
    for run in runs:
        flash(f"{run.name}: {run.message}", "success" if run.status == STATUS_OK else "error")
    return _bank_page(company_id)
