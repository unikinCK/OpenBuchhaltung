"""Täglicher automatischer Umsatzabruf eingerichteter Bankzugänge über die API.

``GET /bank-auto-syncs`` listet die Zugänge mit eingeschaltetem Abruf samt Stand des
letzten Laufs. Eingeschaltet wird je Quelle: ``POST /fints-connections/<id>/auto-sync``
(mit PIN, die dafür verschlüsselt gespeichert wird), ``POST /open-banking-connections/<id>/
auto-sync`` und ``POST /bank-accounts/<id>/mcp-auto-sync`` (Server und Wallet).
``POST /bank-auto-syncs/<id>/disable`` schaltet aus (und löscht eine gespeicherte PIN),
``POST /bank-auto-syncs/run`` ruft sofort ab statt auf den täglichen Lauf zu warten.
"""

from __future__ import annotations

from flask import current_app, jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.bank_auto_sync import (
    BankAutoSyncError,
    disable_bank_auto_sync,
    enable_fints_auto_sync,
    enable_mcp_auto_sync,
    enable_open_banking_auto_sync,
    list_bank_auto_syncs,
    run_bank_auto_syncs,
    serialize_auto_sync,
    sync_time,
)
from app.services.mcp_bank_sync import McpBankSyncError, ServerCaller
from domain.models import Account, BankAutoSync, FinTSConnection, OpenBankingConnection


def _changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _user_id() -> int | None:
    return (current_api_user() or {}).get("id")


def _schedule() -> str:
    try:
        return f"{sync_time(current_app.config):%H:%M}"
    except BankAutoSyncError:
        return str(current_app.config.get("BANK_AUTO_SYNC_TIME"))


@api_bp.get("/bank-auto-syncs")
def list_bank_auto_syncs_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}
    with get_session_factory()() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        entries = list_bank_auto_syncs(
            session, company_id=company_id, include_inactive=include_inactive
        )
        return jsonify(
            {
                "company_id": company_id,
                "daily_at": _schedule(),
                "timezone": "Europe/Berlin",
                "auto_syncs": [serialize_auto_sync(entry) for entry in entries],
            }
        )


@api_bp.post("/fints-connections/<int:connection_id>/auto-sync")
def enable_fints_auto_sync_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        connection = session.get(FinTSConnection, connection_id)
        if connection is None or api_scoped_company(session, connection.company_id) is None:
            return jsonify({"error": "FinTS connection not found."}), 404
        try:
            entry = enable_fints_auto_sync(
                session,
                connection_id=connection.id,
                pin=str(payload.get("pin") or ""),
                secret_key=current_app.config.get("SECRET_KEY"),
                user_id=_user_id(),
                changed_by=_changed_by(),
            )
        except BankAutoSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_auto_sync(entry)), 200


@api_bp.post("/open-banking-connections/<int:connection_id>/auto-sync")
def enable_open_banking_auto_sync_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()
    with get_session_factory()() as session:
        connection = session.get(OpenBankingConnection, connection_id)
        if connection is None or api_scoped_company(session, connection.company_id) is None:
            return jsonify({"error": "Open banking connection not found."}), 404
        try:
            entry = enable_open_banking_auto_sync(
                session,
                connection_id=connection.id,
                user_id=_user_id(),
                changed_by=_changed_by(),
            )
        except BankAutoSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_auto_sync(entry)), 200


@api_bp.post("/bank-accounts/<int:bank_account_id>/mcp-auto-sync")
def enable_mcp_auto_sync_via_api(bank_account_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    server_id = payload.get("server_id")
    wallet_id = payload.get("wallet_id")
    if not isinstance(server_id, int) or isinstance(server_id, bool):
        return jsonify({"error": "server_id (integer) is required."}), 400
    if not isinstance(wallet_id, str) or not wallet_id.strip():
        return jsonify({"error": "wallet_id (string) is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        account = session.get(Account, bank_account_id)
        company = api_scoped_company(session, account.company_id) if account else None
        if company is None:
            return jsonify({"error": "Bank account not found."}), 404
        try:
            with ServerCaller(
                session_factory=session_factory,
                server_id=server_id,
                tenant_id=company.tenant_id,
                company_id=company.id,
                user=current_api_user(),
            ) as caller:
                entry = enable_mcp_auto_sync(
                    session,
                    company=company,
                    caller=caller,
                    server_id=server_id,
                    bank_account_id=bank_account_id,
                    wallet_id=wallet_id.strip(),
                    user_id=_user_id(),
                    changed_by=_changed_by(),
                )
        except (BankAutoSyncError, McpBankSyncError) as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_auto_sync(entry)), 200


@api_bp.post("/bank-auto-syncs/<int:auto_sync_id>/disable")
def disable_bank_auto_sync_via_api(auto_sync_id: int):
    if not api_can_write():
        return forbidden()
    with get_session_factory()() as session:
        entry = session.get(BankAutoSync, auto_sync_id)
        if entry is None or api_scoped_company(session, entry.company_id) is None:
            return jsonify({"error": "Auto sync not found."}), 404
        entry = disable_bank_auto_sync(
            session, auto_sync_id=entry.id, changed_by=_changed_by()
        )
        return jsonify(serialize_auto_sync(entry)), 200


@api_bp.post("/bank-auto-syncs/run")
def run_bank_auto_syncs_via_api():
    """Sofortiger Abruf aller (oder eines) eingeschalteten Zugänge einer Gesellschaft."""
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    auto_sync_id = payload.get("auto_sync_id")
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    if auto_sync_id is not None and (
        not isinstance(auto_sync_id, int) or isinstance(auto_sync_id, bool)
    ):
        return jsonify({"error": "auto_sync_id must be an integer."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        company_id = company.id
    runs = run_bank_auto_syncs(
        session_factory,
        config=current_app.config,
        company_id=company_id,
        auto_sync_id=auto_sync_id,
    )
    return jsonify({"company_id": company_id, "runs": [run.to_dict() for run in runs]})
