"""Bankumsätze über den MCP-Server der Bank abrufen (z. B. Finom), ohne KI.

``GET /mcp-bank-sources`` listet die nutzbaren MCP-Server, ``GET /mcp-servers/<id>/
bank-wallets`` die Wallets (Konten) beim Anbieter samt Vorschlag für ein Bankkonto,
``GET /bank-accounts/<id>/mcp-sync/preview`` zeigt die neuen Umsätze, ohne etwas zu
schreiben, ``POST /bank-accounts/<id>/mcp-sync`` ruft ab und importiert.

Der Abruf nutzt den Zugang des aufrufenden Benutzers zum MCP-Server (wie der KI-Chat);
ohne Benutzer (globaler API-Token) gilt nur ein zentraler Zugang. Wie Chat und Proxy nur
für Benutzer mit Schreibrecht: Die Umsätze sind Bankdaten.
"""

from __future__ import annotations

from datetime import date

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.mcp_bank_sync import (
    McpBankSyncError,
    ServerCaller,
    bank_adapter,
    bank_sync_servers,
    default_sync_range,
    default_wallet,
    list_wallets,
    resolve_bank_account,
    resolve_server,
    sync_mcp_bank_transactions,
)
from domain.models import Account


def _changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise McpBankSyncError(f"{field} muss ein ISO-Datum (YYYY-MM-DD) sein.") from exc


def _caller(server_id: int, company) -> ServerCaller:
    return ServerCaller(
        session_factory=get_session_factory(),
        server_id=server_id,
        tenant_id=company.tenant_id,
        company_id=company.id,
        user=current_api_user(),
    )


@api_bp.get("/mcp-bank-sources")
def list_mcp_bank_sources():
    """MCP-Server, über die sich Bankumsätze der Gesellschaft abrufen lassen."""
    if not api_can_write():
        return forbidden()
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        servers = bank_sync_servers(session, tenant_id=company.tenant_id)
        return jsonify(
            {
                "company_id": company.id,
                "sources": [
                    {"server_id": server.id, "name": server.name, "adapter": bank_adapter(server)}
                    for server in servers
                ],
            }
        ), 200


@api_bp.get("/mcp-servers/<int:server_id>/bank-wallets")
def list_mcp_bank_wallets(server_id: int):
    """Wallets beim Anbieter; mit ``bank_account_id`` zusätzlich Wallet- und Zeitraumvorschlag."""
    if not api_can_write():
        return forbidden()
    company_id = request.args.get("company_id", type=int)
    bank_account_id = request.args.get("bank_account_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            server = resolve_server(session, company, server_id)
            account = (
                resolve_bank_account(session, company, bank_account_id)
                if bank_account_id
                else None
            )
            with _caller(server.id, company) as caller:
                wallets = list_wallets(caller)
        except McpBankSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        body = {
            "server_id": server.id,
            "server": server.name,
            "wallets": [wallet.to_dict() for wallet in wallets],
        }
        if account is not None:
            suggested = default_wallet(
                wallets,
                session=session,
                company_id=company.id,
                bank_account_id=account.id,
                server_id=server.id,
            )
            from_date, to_date = default_sync_range(
                session, company_id=company.id, bank_account_id=account.id
            )
            body.update(
                bank_account_id=account.id,
                suggested_wallet_id=suggested.id if suggested else None,
                default_from_date=from_date.isoformat(),
                default_to_date=to_date.isoformat(),
            )
        return jsonify(body), 200


def _run_sync(bank_account_id: int, params: dict, *, dry_run: bool):
    server_id = params.get("server_id")
    if isinstance(server_id, str) and server_id.isdigit():
        server_id = int(server_id)
    if not isinstance(server_id, int) or isinstance(server_id, bool):
        return jsonify({"error": "server_id (integer) is required."}), 400
    wallet_id = params.get("wallet_id")
    if wallet_id is not None and not isinstance(wallet_id, str):
        return jsonify({"error": "wallet_id must be a string."}), 400
    with get_session_factory()() as session:
        account = session.get(Account, bank_account_id)
        company = api_scoped_company(session, account.company_id) if account else None
        if company is None:
            return jsonify({"error": "Bank account not found."}), 404
        try:
            from_date = _date(params.get("from_date"), "from_date")
            to_date = _date(params.get("to_date"), "to_date")
            server = resolve_server(session, company, server_id)
            with _caller(server.id, company) as caller:
                result = sync_mcp_bank_transactions(
                    session=session,
                    caller=caller,
                    company=company,
                    server=server,
                    bank_account_id=bank_account_id,
                    wallet_id=wallet_id or None,
                    from_date=from_date,
                    to_date=to_date,
                    dry_run=dry_run,
                    changed_by=_changed_by(),
                )
        except McpBankSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(result.to_dict()), 200


@api_bp.get("/bank-accounts/<int:bank_account_id>/mcp-sync/preview")
def preview_mcp_bank_transactions_via_api(bank_account_id: int):
    """Neue Umsätze einer Wallet anzeigen, ohne etwas zu importieren."""
    if not api_can_write():
        return forbidden()
    return _run_sync(bank_account_id, request.args.to_dict(), dry_run=True)


@api_bp.post("/bank-accounts/<int:bank_account_id>/mcp-sync")
def sync_mcp_bank_transactions_via_api(bank_account_id: int):
    """Umsätze einer Wallet abrufen und importieren."""
    if not api_can_write():
        return forbidden()
    return _run_sync(bank_account_id, request.get_json(silent=True) or {}, dry_run=False)
