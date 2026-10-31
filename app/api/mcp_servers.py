"""Externe MCP-Server für Obi über die API: Verwaltung und Zugänge.

Anlegen, Ändern, Löschen und den zentralen Zugang (``scope=global``) pflegt ein
Administrator; persönliche Zugänge (``scope=user``) jeder Benutzer selbst. Die
OAuth-Anmeldung läuft im Browser: ``…/oauth/start`` liefert die Anmelde-URL, der
Rücksprung landet auf der Weboberfläche (``/obi-verbindungen/oauth/callback``) und
verlangt dort die Anmeldung desselben Benutzers.
"""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, forbidden, get_session_factory
from app.auth import current_api_tenant_id, current_api_user
from app.services.mcp_connections import (
    McpConnectionError,
    can_manage_server,
    clear_credential,
    create_server,
    delete_server,
    oauth_redirect_uri,
    refresh_tools,
    serialize_server,
    server_visible,
    set_token_credential,
    start_oauth,
    try_refresh_tools,
    update_server,
    visible_servers,
)
from domain.models import McpServer, Tenant

_SETTING_FIELDS = (
    "name",
    "tool_prefix",
    "url",
    "description",
    "auth_type",
    "credential_scope",
    "token_header",
    "oauth_client_id",
    "oauth_client_secret",
    "oauth_scope",
    "oauth_authorization_url",
    "oauth_token_url",
    "confirm_mode",
    "allowed_tools",
    "timeout_seconds",
    "is_active",
)


def _actor() -> str:
    user = current_api_user()
    return user["username"] if user else "api-token"


def _user_id() -> int | None:
    user = current_api_user()
    return user["id"] if user else None


def _not_found():
    return jsonify({"error": "MCP server not found."}), 404


def _scoped_server(session, server_id: int) -> McpServer | None:
    server = session.get(McpServer, server_id)
    if server is None or not server_visible(current_api_user(), server):
        return None
    return server


def _usable_server(session, server_id: int) -> McpServer | None:
    """Sichtbarer Server; inaktive nur für Administratoren mit Zugriff."""
    server = _scoped_server(session, server_id)
    if server is None:
        return None
    if not server.is_active and not can_manage_server(current_api_user(), server):
        return None
    return server


def _settings(payload: dict) -> dict:
    return {key: payload[key] for key in _SETTING_FIELDS if key in payload}


@api_bp.get("/mcp-servers")
def list_mcp_servers():
    """MCP-Server im Zugriffsbereich; Administratoren mit Einstellungen, alle mit Zugangsstatus."""
    user = current_api_user()
    session_factory = get_session_factory()
    with session_factory() as session:
        servers = visible_servers(session, user, active_only=not can_manage_server(user))
        return jsonify(
            {"mcp_servers": [serialize_server(session, server, user=user) for server in servers]}
        ), 200


@api_bp.post("/mcp-servers")
def create_mcp_server():
    user = current_api_user()
    if not can_manage_server(user):
        return forbidden()
    payload = request.get_json(silent=True) or {}
    tenant_scope = current_api_tenant_id()
    tenant_id = payload.get("tenant_id")
    if tenant_scope is not None:
        if tenant_id not in (None, tenant_scope):
            return jsonify({"error": "Tenant is outside your scope."}), 403
        tenant_id = tenant_scope
    elif tenant_id is not None and not isinstance(tenant_id, int):
        return jsonify({"error": "tenant_id must be an integer or null."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if tenant_id is not None and session.get(Tenant, tenant_id) is None:
            return jsonify({"error": "Tenant not found."}), 404
        try:
            server = create_server(
                session,
                data=_settings(payload),
                tenant_id=tenant_id,
                actor=_actor(),
                allow_internal=tenant_scope is None,
            )
        except McpConnectionError as exc:
            return jsonify({"error": str(exc)}), 400
        session.commit()
        return jsonify(serialize_server(session, server, user=user)), 201


@api_bp.patch("/mcp-servers/<int:server_id>")
def update_mcp_server(server_id: int):
    user = current_api_user()
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        server = _scoped_server(session, server_id)
        if server is None:
            return _not_found()
        if not can_manage_server(user, server):
            return forbidden()
        try:
            reset = update_server(
                session,
                server,
                data=_settings(payload),
                actor=_actor(),
                allow_internal=current_api_tenant_id() is None,
            )
        except McpConnectionError as exc:
            return jsonify({"error": str(exc)}), 400
        session.commit()
        body = serialize_server(session, server, user=user)
        body["credentials_reset"] = reset
        return jsonify(body), 200


@api_bp.post("/mcp-servers/<int:server_id>/delete")
def delete_mcp_server(server_id: int):
    user = current_api_user()
    session_factory = get_session_factory()
    with session_factory() as session:
        server = _scoped_server(session, server_id)
        if server is None:
            return _not_found()
        if not can_manage_server(user, server):
            return forbidden()
        delete_server(session, server, actor=_actor())
        session.commit()
        return jsonify({"deleted": True, "id": server_id}), 200


@api_bp.post("/mcp-servers/<int:server_id>/tools/refresh")
def refresh_mcp_server_tools(server_id: int):
    """Werkzeugliste neu abrufen (mit dem zentralen bzw. dem eigenen Zugang)."""
    user = current_api_user()
    session_factory = get_session_factory()
    with session_factory() as session:
        server = _scoped_server(session, server_id)
        if server is None:
            return _not_found()
        if not can_manage_server(user, server):
            return forbidden()
        try:
            refresh_tools(session, server, user_id=_user_id())
        except McpConnectionError as exc:
            session.commit()
            return jsonify({"error": str(exc)}), 502
        session.commit()
        return jsonify(serialize_server(session, server, user=user)), 200


def _credential_scope(payload: dict, server: McpServer) -> str:
    return str(payload.get("scope") or server.credential_scope).strip().lower()


def _may_change_credential(server: McpServer, scope: str) -> bool:
    """Zentraler Zugang: Administrator; eigener Zugang: Benutzer mit Schreibrecht."""
    if scope == "global":
        return can_manage_server(current_api_user(), server)
    return api_can_write()


@api_bp.post("/mcp-servers/<int:server_id>/credential")
def set_mcp_server_token(server_id: int):
    """Token als zentralen (``scope=global``) oder eigenen (``scope=user``) Zugang setzen."""
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        server = _usable_server(session, server_id)
        if server is None:
            return _not_found()
        scope = _credential_scope(payload, server)
        if not _may_change_credential(server, scope):
            return forbidden()
        user = current_api_user()
        try:
            set_token_credential(
                session,
                server,
                scope=scope,
                user_id=_user_id(),
                token=payload.get("token"),
                actor=_actor(),
                actor_tenant_id=user.get("tenant_id") if user else None,
            )
        except McpConnectionError as exc:
            return jsonify({"error": str(exc)}), 400
        session.commit()
        # Werkzeuge gleich mit dem neuen Zugang abrufen: prüft zugleich das Token.
        refresh_error = try_refresh_tools(session, server, user=user)
        session.commit()
        body = serialize_server(session, server, user=user)
        if refresh_error:
            body["tools_refresh_error"] = refresh_error
        return jsonify(body), 200


@api_bp.post("/mcp-servers/<int:server_id>/credential/delete")
def clear_mcp_server_credential(server_id: int):
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        server = _scoped_server(session, server_id)
        if server is None:
            return _not_found()
        scope = _credential_scope(payload, server)
        if not _may_change_credential(server, scope):
            return forbidden()
        user = current_api_user()
        try:
            removed = clear_credential(
                session,
                server,
                scope=scope,
                user_id=_user_id(),
                actor=_actor(),
                actor_tenant_id=user.get("tenant_id") if user else None,
            )
        except McpConnectionError as exc:
            return jsonify({"error": str(exc)}), 400
        session.commit()
        body = serialize_server(session, server, user=user)
        body["removed"] = removed
        return jsonify(body), 200


@api_bp.post("/mcp-servers/<int:server_id>/oauth/start")
def start_mcp_server_oauth(server_id: int):
    """OAuth-Anmeldung beginnen: liefert die URL, die der Benutzer im Browser öffnet."""
    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        server = _usable_server(session, server_id)
        if server is None:
            return _not_found()
        scope = _credential_scope(payload, server)
        if not _may_change_credential(server, scope):
            return forbidden()
        redirect_uri = oauth_redirect_uri(prefer_issuer=True)
        try:
            url = start_oauth(
                session,
                server,
                scope=scope,
                user_id=_user_id(),
                redirect_uri=redirect_uri,
                actor=_actor(),
            )
        except McpConnectionError as exc:
            session.commit()
            return jsonify({"error": str(exc)}), 422
        session.commit()
        return jsonify(
            {
                "status": "authorization_pending",
                "server_id": server.id,
                "authorization_url": url,
                "redirect_uri": redirect_uri,
                "hint": (
                    "authorization_url im Browser öffnen und bei OpenBuchhaltung mit demselben "
                    "Benutzer angemeldet sein; der Rücksprung schließt die Anmeldung ab."
                ),
            }
        ), 200
