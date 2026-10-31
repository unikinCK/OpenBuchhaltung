"""Externe MCP-Server für Obi in der Oberfläche.

Verwaltung → MCP-Server (Administratoren): Server anlegen und ändern, Werkzeuge
abrufen und freigeben, zentralen Zugang pflegen. Benutzermenü → Obi-Verbindungen:
eigene Anmeldung bei Servern mit Zugängen je Benutzer.

Die OAuth-Anmeldung führt über eine Zwischenseite zum Anbieter – ein Formular darf
wegen ``form-action 'self'`` (CSP) nicht auf einen fremden Host weiterleiten – und
kommt auf ``/obi-verbindungen/oauth/callback`` zurück.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import abort, flash, redirect, render_template, request, url_for

from app.auth import current_user
from app.services.mcp_connections import (
    AUTH_LABELS,
    CONFIRM_LABELS,
    SCOPE_GLOBAL,
    SCOPE_LABELS,
    SCOPE_USER,
    McpConnectionError,
    abort_oauth,
    can_manage_server,
    clear_credential,
    complete_oauth,
    create_server,
    delete_server,
    find_pending_credential,
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
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import get_session_factory
from domain.models import McpServer, Tenant

STATUS_BADGES = {
    "connected": "badge-success",
    "pending": "badge-info",
    "not_connected": "badge-warning",
    "expired": "badge-danger",
    "error": "badge-danger",
}


def _admin_page(server_id: int | None = None):
    anchor = f"#mcp-{server_id}" if server_id else ""
    return redirect(url_for("main.mcp_servers_page") + anchor)


def _connections_page():
    return redirect(url_for("main.obi_connections_page"))


def _form_settings() -> dict:
    form = request.form
    secret = None if form.get("oauth_client_secret_clear") else form.get("oauth_client_secret", "")
    return {
        "name": form.get("name"),
        "tool_prefix": form.get("tool_prefix"),
        "url": form.get("url"),
        "description": form.get("description"),
        "auth_type": form.get("auth_type") or "none",
        "credential_scope": form.get("credential_scope") or SCOPE_GLOBAL,
        "token_header": form.get("token_header"),
        "oauth_client_id": form.get("oauth_client_id"),
        "oauth_client_secret": secret,
        "oauth_scope": form.get("oauth_scope"),
        "oauth_authorization_url": form.get("oauth_authorization_url"),
        "oauth_token_url": form.get("oauth_token_url"),
        "confirm_mode": form.get("confirm_mode") or "write",
        "timeout_seconds": form.get("timeout_seconds"),
    }


def _managed_server(session, server_id: int) -> McpServer:
    server = session.get(McpServer, server_id)
    user = current_user()
    if server is None or not server_visible(user, server) or not can_manage_server(user, server):
        abort(404)
    return server


def _usable_server(session, server_id: int) -> McpServer:
    server = session.get(McpServer, server_id)
    if server is None or not server.is_active or not server_visible(current_user(), server):
        abort(404)
    return server


def _flash_credential_saved(message: str, session, server: McpServer, user: dict) -> None:
    """Werkzeuge mit dem neuen Zugang abrufen und das Ergebnis melden (prüft den Zugang)."""
    error = try_refresh_tools(session, server, user=user)
    session.commit()
    if error:
        flash(f"{message}, aber der Server war nicht abrufbar: {error}", "error")
    elif server.tools_cache is None:
        flash(f"{message} – der Zugang funktioniert.", "success")
    else:
        flash(f"{message} – {len(server.tools_cache)} Werkzeuge verfügbar.", "success")


def _render_oauth_redirect(server_name: str, authorization_url: str):
    return render_template(
        "mcp_oauth_redirect.html",
        server_name=server_name,
        authorization_url=authorization_url,
        authorization_host=urlsplit(authorization_url).hostname,
        companies=[],
        selected_company_id=None,
    )


# ---------------------------------------------------------------------------
# Verwaltung → MCP-Server
# ---------------------------------------------------------------------------


@main_bp.get("/verwaltung/mcp-server")
def mcp_servers_page():
    user = current_user()
    is_admin = can_manage_server(user)
    servers: list[dict] = []
    tenants: list[Tenant] = []
    with get_session_factory()() as session:
        if is_admin:
            servers = [
                serialize_server(session, server, user=user)
                for server in visible_servers(session, user)
            ]
            if user.get("tenant_id") is None:
                tenants = (
                    session.execute(scoped_select(Tenant).order_by(Tenant.name)).scalars().all()
                )
    return render_template(
        "mcp_server.html",
        is_admin=is_admin,
        is_global_admin=is_admin and user.get("tenant_id") is None,
        servers=servers,
        tenants=tenants,
        redirect_uri=oauth_redirect_uri(),
        auth_labels=AUTH_LABELS,
        scope_labels=SCOPE_LABELS,
        confirm_labels=CONFIRM_LABELS,
        status_badges=STATUS_BADGES,
        companies=[],
        selected_company_id=None,
    )


@main_bp.post("/verwaltung/mcp-server")
def create_mcp_server_action():
    user = current_user()
    if not can_manage_server(user):
        flash("MCP-Server verwalten kann nur ein Administrator.", "error")
        return _admin_page()
    tenant_id = user.get("tenant_id")
    if tenant_id is None:
        tenant_id = request.form.get("tenant_id", type=int)
    with get_session_factory()() as session:
        if tenant_id is not None and session.get(Tenant, tenant_id) is None:
            flash("Mandant wurde nicht gefunden.", "error")
            return _admin_page()
        try:
            server = create_server(
                session,
                data=_form_settings(),
                tenant_id=tenant_id,
                actor=user["username"],
                allow_internal=user.get("tenant_id") is None,
            )
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _admin_page()
        session.commit()
        server_id, name = server.id, server.name
    flash(
        f"MCP-Server „{name}“ wurde angelegt – jetzt anmelden bzw. Werkzeuge abrufen.",
        "success",
    )
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>")
def update_mcp_server_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        try:
            reset = update_server(
                session,
                server,
                data=_form_settings(),
                actor=user["username"],
                allow_internal=user.get("tenant_id") is None,
            )
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _admin_page(server_id)
        session.commit()
    message = "Einstellungen wurden gespeichert."
    if reset:
        message += " Bestehende Anmeldungen wurden entfernt – bitte neu anmelden."
    flash(message, "success")
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/werkzeuge")
def refresh_mcp_server_tools_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        try:
            tools = refresh_tools(session, server, user_id=user["id"])
        except McpConnectionError as exc:
            session.commit()
            flash(f"Werkzeuge konnten nicht abgerufen werden: {exc}", "error")
            return _admin_page(server_id)
        session.commit()
    flash(f"{len(tools)} Werkzeuge abgerufen.", "success")
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/freigabe")
def set_mcp_server_tools_action(server_id: int):
    """Freigegebene Werkzeuge speichern (``all_tools`` = alle, auch künftige)."""
    user = current_user()
    allowed = None if request.form.get("all_tools") else request.form.getlist("tools")
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        try:
            update_server(session, server, data={"allowed_tools": allowed}, actor=user["username"])
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _admin_page(server_id)
        session.commit()
    flash("Freigabe der Werkzeuge wurde gespeichert.", "success")
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/aktiv")
def set_mcp_server_active_action(server_id: int):
    user = current_user()
    is_active = request.form.get("is_active") == "true"
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        update_server(session, server, data={"is_active": is_active}, actor=user["username"])
        session.commit()
    state = "aktiviert" if is_active else "deaktiviert"
    flash(f"MCP-Server wurde {state}.", "success")
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/loeschen")
def delete_mcp_server_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        name = server.name
        delete_server(session, server, actor=user["username"])
        session.commit()
    flash(f"MCP-Server „{name}“ wurde gelöscht.", "success")
    return _admin_page()


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/zugang")
def set_mcp_server_global_token_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        try:
            set_token_credential(
                session,
                server,
                scope=SCOPE_GLOBAL,
                user_id=user["id"],
                token=request.form.get("token"),
                actor=user["username"],
                actor_tenant_id=user.get("tenant_id"),
            )
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _admin_page(server_id)
        session.commit()
        _flash_credential_saved("Zentraler Zugang wurde gespeichert", session, server, user)
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/zugang/loeschen")
def clear_mcp_server_global_credential_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        try:
            clear_credential(
                session,
                server,
                scope=SCOPE_GLOBAL,
                user_id=user["id"],
                actor=user["username"],
                actor_tenant_id=user.get("tenant_id"),
            )
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _admin_page(server_id)
        session.commit()
    flash("Zentraler Zugang wurde entfernt.", "success")
    return _admin_page(server_id)


@main_bp.post("/verwaltung/mcp-server/<int:server_id>/oauth")
def start_mcp_server_global_oauth_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _managed_server(session, server_id)
        try:
            url = start_oauth(
                session,
                server,
                scope=SCOPE_GLOBAL,
                user_id=user["id"],
                redirect_uri=oauth_redirect_uri(),
                actor=user["username"],
            )
        except McpConnectionError as exc:
            session.commit()
            flash(f"Die Anmeldung konnte nicht gestartet werden: {exc}", "error")
            return _admin_page(server_id)
        session.commit()
        name = server.name
    return _render_oauth_redirect(name, url)


# ---------------------------------------------------------------------------
# Benutzermenü → Obi-Verbindungen
# ---------------------------------------------------------------------------


@main_bp.get("/obi-verbindungen")
def obi_connections_page():
    user = current_user()
    with get_session_factory()() as session:
        servers = [
            serialize_server(session, server, user=user)
            for server in visible_servers(session, user, active_only=True)
        ]
    return render_template(
        "obi_verbindungen.html",
        servers=servers,
        is_admin=can_manage_server(user),
        status_badges=STATUS_BADGES,
        companies=[],
        selected_company_id=None,
    )


@main_bp.post("/obi-verbindungen/<int:server_id>/token")
def set_own_mcp_token_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _usable_server(session, server_id)
        try:
            set_token_credential(
                session,
                server,
                scope=SCOPE_USER,
                user_id=user["id"],
                token=request.form.get("token"),
                actor=user["username"],
                actor_tenant_id=user.get("tenant_id"),
            )
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _connections_page()
        session.commit()
        _flash_credential_saved(
            f"Zugang zu „{server.name}“ wurde gespeichert", session, server, user
        )
    return _connections_page()


@main_bp.post("/obi-verbindungen/<int:server_id>/oauth")
def start_own_mcp_oauth_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _usable_server(session, server_id)
        try:
            url = start_oauth(
                session,
                server,
                scope=SCOPE_USER,
                user_id=user["id"],
                redirect_uri=oauth_redirect_uri(),
                actor=user["username"],
            )
        except McpConnectionError as exc:
            session.commit()
            flash(f"Die Anmeldung konnte nicht gestartet werden: {exc}", "error")
            return _connections_page()
        session.commit()
        name = server.name
    return _render_oauth_redirect(name, url)


@main_bp.post("/obi-verbindungen/<int:server_id>/abmelden")
def clear_own_mcp_credential_action(server_id: int):
    user = current_user()
    with get_session_factory()() as session:
        server = _usable_server(session, server_id)
        try:
            clear_credential(
                session,
                server,
                scope=SCOPE_USER,
                user_id=user["id"],
                actor=user["username"],
                actor_tenant_id=user.get("tenant_id"),
            )
        except McpConnectionError as exc:
            flash(str(exc), "error")
            return _connections_page()
        session.commit()
        name = server.name
    flash(f"Abgemeldet von „{name}“.", "success")
    return _connections_page()


@main_bp.get("/obi-verbindungen/oauth/callback")
def mcp_oauth_callback():
    """Rücksprung vom Anbieter (``code``/``state`` bzw. ``error``)."""
    user = current_user()
    state = request.args.get("state") or ""
    with get_session_factory()() as session:
        pending = find_pending_credential(session, state)
        global_login = pending is not None and pending.user_id is None
        target = _admin_page(pending.server_id) if global_login else _connections_page()
        error = request.args.get("error")
        if error:
            abort_oauth(session, state=state, user_id=user["id"])
            session.commit()
            description = request.args.get("error_description") or error
            flash(f"Die Anmeldung wurde nicht abgeschlossen: {description[:200]}", "error")
            return target
        try:
            credential = complete_oauth(
                session,
                state=state,
                code=request.args.get("code") or "",
                user_id=user["id"],
                actor=user["username"],
                actor_tenant_id=user.get("tenant_id"),
                iss=request.args.get("iss"),
            )
        except McpConnectionError as exc:
            session.commit()
            flash(str(exc), "error")
            return target
        session.commit()
        server = credential.server
        _flash_credential_saved(f"Anmeldung bei „{server.name}“ erfolgreich", session, server, user)
    return target
