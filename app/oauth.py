"""OAuth 2.1 Authorization Server für den MCP-Endpunkt (z. B. ChatGPT-, Claude-Connectoren).

Umgesetzt nach der MCP-Autorisierungsspezifikation:

* Discovery: Protected Resource Metadata (RFC 9728) unter
  ``/.well-known/oauth-protected-resource[/mcp]`` und Authorization Server Metadata
  (RFC 8414) unter ``/.well-known/oauth-authorization-server``.
* Dynamic Client Registration (RFC 7591): ``POST /oauth/register``.
* Authorization-Code-Flow mit PKCE (nur ``S256``): ``/oauth/authorize`` verlangt die
  normale UI-Anmeldung und eine ausdrückliche Zustimmung des Benutzers.
* Token-Endpunkt ``POST /oauth/token`` (``authorization_code``, ``refresh_token`` mit
  Rotation) und Widerruf ``POST /oauth/revoke`` (RFC 7009).

Die ausgegebenen Access-Tokens wirken wie Benutzer-API-Tokens: Die REST-API (und damit
der MCP-Server, der sie durchreicht) arbeitet mit Rolle und Mandant des Benutzers.
Tokens werden nur als SHA-256 gespeichert.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import select

from app.auth import (
    _get_session_factory,
    constant_time_equals,
    current_user,
    hash_api_token,
    validate_csrf,
)
from app.services.security_events import record_security_event
from domain.models import OAuthAuthorizationCode, OAuthClient, OAuthGrant, User

oauth_bp = Blueprint("oauth", __name__)

MCP_RESOURCE_PATH = "/mcp"
SUPPORTED_SCOPES = ("mcp",)
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
AUTHORIZATION_CODE_SECONDS = 600
ACCESS_TOKEN_PREFIX = "obo_"
REFRESH_TOKEN_PREFIX = "obr_"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite liefert naive Zeitstempel zurück; als UTC interpretieren."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def oauth_enabled() -> bool:
    return bool(current_app.config.get("OAUTH_ENABLED", True))


@oauth_bp.before_request
def _require_enabled():
    if not oauth_enabled():
        abort(404)


def issuer_url() -> str:
    configured = (current_app.config.get("OAUTH_ISSUER") or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.url_root.rstrip("/")


def resource_url() -> str:
    return f"{issuer_url()}{MCP_RESOURCE_PATH}"


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _oauth_error(error: str, description: str, status: int = 400):
    response = jsonify({"error": error, "error_description": description})
    response.status_code = status
    return _no_store(response)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@oauth_bp.get("/.well-known/oauth-protected-resource")
@oauth_bp.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata():
    return jsonify(
        {
            "resource": resource_url(),
            "authorization_servers": [issuer_url()],
            "scopes_supported": list(SUPPORTED_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": "OpenBuchhaltung MCP",
        }
    )


@oauth_bp.get("/.well-known/oauth-authorization-server")
@oauth_bp.get("/.well-known/oauth-authorization-server/mcp")
def authorization_server_metadata():
    issuer = issuer_url()
    return jsonify(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "revocation_endpoint": f"{issuer}/oauth/revoke",
            "scopes_supported": list(SUPPORTED_SCOPES),
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "code_challenge_methods_supported": ["S256"],
        }
    )


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------


def _allowed_redirect_hosts() -> set[str]:
    raw = current_app.config.get("OAUTH_ALLOWED_REDIRECT_HOSTS") or ""
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def redirect_uri_error(uri: str) -> str | None:
    """Prüft eine Redirect-URI: HTTPS (oder HTTP auf Loopback), kein Fragment."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return "redirect_uri fehlt oder ist zu lang."
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.fragment:
        return "redirect_uri darf kein Fragment enthalten."
    if parts.scheme == "https" and host:
        pass
    elif parts.scheme == "http" and host in LOOPBACK_HOSTS:
        pass
    else:
        return "redirect_uri muss HTTPS verwenden (HTTP nur für localhost)."
    allowed = _allowed_redirect_hosts()
    if allowed and host not in allowed and host not in LOOPBACK_HOSTS:
        return f"Redirect-Host {host} ist nicht freigegeben."
    return None


@oauth_bp.post("/oauth/register")
def register_client():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _oauth_error("invalid_client_metadata", "JSON-Body erwartet.")

    redirect_uris = payload.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or len(redirect_uris) > 10:
        return _oauth_error("invalid_redirect_uri", "redirect_uris (1–10 URIs) erforderlich.")
    for uri in redirect_uris:
        error = redirect_uri_error(uri)
        if error:
            return _oauth_error("invalid_redirect_uri", error)

    auth_method = payload.get("token_endpoint_auth_method") or "none"
    if auth_method not in AUTH_METHODS:
        return _oauth_error(
            "invalid_client_metadata",
            f"token_endpoint_auth_method {auth_method} nicht unterstützt.",
        )
    grant_types = payload.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        return _oauth_error(
            "invalid_client_metadata", "grant_types muss authorization_code enthalten."
        )
    response_types = payload.get("response_types") or ["code"]
    if response_types != ["code"]:
        return _oauth_error("invalid_client_metadata", "Nur response_types ['code'] unterstützt.")

    client_name = str(payload.get("client_name") or "Unbenannter Client").strip()[:200]
    client_id = f"obc_{secrets.token_urlsafe(24)}"
    client_secret = None
    if auth_method != "none":
        client_secret = secrets.token_urlsafe(32)

    with _get_session_factory()() as db_session:
        client = OAuthClient(
            client_id=client_id,
            client_secret_hash=hash_api_token(client_secret) if client_secret else None,
            client_name=client_name or "Unbenannter Client",
            redirect_uris=list(redirect_uris),
            token_endpoint_auth_method=auth_method,
        )
        db_session.add(client)
        db_session.commit()
        issued_at = int(client.created_at.timestamp())

    body: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
        "client_name": client_name,
        "redirect_uris": list(redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
    }
    if client_secret:
        body["client_secret"] = client_secret
        body["client_secret_expires_at"] = 0
    response = jsonify(body)
    response.status_code = 201
    return _no_store(response)


# ---------------------------------------------------------------------------
# Autorisierung (Login + Zustimmung)
# ---------------------------------------------------------------------------


def _load_client(db_session, client_id: str | None) -> OAuthClient | None:
    if not client_id:
        return None
    return db_session.execute(
        select(OAuthClient).where(OAuthClient.client_id == client_id)
    ).scalar_one_or_none()


def _authorization_params(source) -> dict[str, str]:
    keys = (
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
        "scope",
        "code_challenge",
        "code_challenge_method",
        "resource",
    )
    return {key: (source.get(key) or "").strip() for key in keys}


def _redirect_with(redirect_uri: str, params: dict[str, str]):
    separator = "&" if "?" in redirect_uri else "?"
    clean = {key: value for key, value in params.items() if value}
    return redirect(f"{redirect_uri}{separator}{urlencode(clean)}")


def _validate_authorization_request(db_session, params: dict[str, str]):
    """Liefert (client, fehler_für_redirect, fehler_ohne_redirect)."""
    client = _load_client(db_session, params["client_id"])
    if client is None:
        return None, None, "Unbekannter OAuth-Client (client_id)."
    redirect_uri = params["redirect_uri"]
    if not redirect_uri and len(client.redirect_uris) == 1:
        redirect_uri = client.redirect_uris[0]
        params["redirect_uri"] = redirect_uri
    if redirect_uri not in client.redirect_uris:
        return client, None, "redirect_uri ist für diesen Client nicht registriert."
    if params["response_type"] != "code":
        return client, ("unsupported_response_type", "Nur response_type=code."), None
    if not params["code_challenge"] or params["code_challenge_method"] != "S256":
        pkce_error = ("invalid_request", "PKCE mit code_challenge_method=S256 erforderlich.")
        return client, pkce_error, None
    if not 43 <= len(params["code_challenge"]) <= 128:
        return client, ("invalid_request", "code_challenge hat eine ungültige Länge."), None
    scopes = params["scope"].split()
    if any(scope not in SUPPORTED_SCOPES for scope in scopes):
        return client, ("invalid_scope", "Unbekannter Scope."), None
    resource = params["resource"]
    if resource and urlsplit(resource).netloc != urlsplit(issuer_url()).netloc:
        return client, ("invalid_target", "resource gehört nicht zu diesem Server."), None
    return client, None, None


def _consent_csp(redirect_uri: str) -> str:
    """CSP der Zustimmungsseite: form-action muss den Redirect zum Client erlauben."""
    parts = urlsplit(redirect_uri)
    origin = f"{parts.scheme}://{parts.netloc}"
    return (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
        f"form-action 'self' {origin}"
    )


@oauth_bp.get("/oauth/authorize")
def authorize_form():
    params = _authorization_params(request.args)
    with _get_session_factory()() as db_session:
        client, redirect_error, fatal_error = _validate_authorization_request(db_session, params)
        if fatal_error:
            return render_template(
                "oauth_error.html", error=fatal_error, companies=[], selected_company_id=None
            ), 400
        if redirect_error:
            return _redirect_with(
                params["redirect_uri"],
                {
                    "error": redirect_error[0],
                    "error_description": redirect_error[1],
                    "state": params["state"],
                },
            )
        client_name = client.client_name

    if current_user() is None:
        flash("Bitte anmelden, um den Zugriff zu erlauben.", "error")
        return redirect(url_for("auth.login_form", next=request.full_path))

    response = current_app.make_response(
        render_template(
            "oauth_authorize.html",
            params=params,
            client_name=client_name,
            redirect_host=urlsplit(params["redirect_uri"]).hostname,
            user=current_user(),
            companies=[],
            selected_company_id=None,
        )
    )
    response.headers["Content-Security-Policy"] = _consent_csp(params["redirect_uri"])
    response.headers["X-Frame-Options"] = "DENY"
    return response


@oauth_bp.post("/oauth/authorize")
def authorize_decision():
    validate_csrf()
    user = current_user()
    if user is None:
        abort(401)
    params = _authorization_params(request.form)
    with _get_session_factory()() as db_session:
        client, redirect_error, fatal_error = _validate_authorization_request(db_session, params)
        if fatal_error:
            return render_template(
                "oauth_error.html", error=fatal_error, companies=[], selected_company_id=None
            ), 400
        if redirect_error:
            return _redirect_with(
                params["redirect_uri"],
                {"error": redirect_error[0], "error_description": redirect_error[1],
                 "state": params["state"]},
            )
        db_user = db_session.get(User, user["id"])
        if request.form.get("decision") != "allow" or db_user is None or not db_user.is_active:
            return _redirect_with(
                params["redirect_uri"],
                {"error": "access_denied", "state": params["state"]},
            )

        code = secrets.token_urlsafe(32)
        db_session.add(
            OAuthAuthorizationCode(
                code_hash=hash_api_token(code),
                client_id=client.id,
                user_id=db_user.id,
                redirect_uri=params["redirect_uri"],
                code_challenge=params["code_challenge"],
                scope=params["scope"] or None,
                resource=params["resource"] or None,
                expires_at=_utcnow() + timedelta(seconds=AUTHORIZATION_CODE_SECONDS),
            )
        )
        record_security_event(
            db_session,
            user=db_user,
            action="oauth_authorized",
            actor=db_user.username,
            payload={"client_id": client.client_id, "client_name": client.client_name},
        )
        db_session.commit()

    return _redirect_with(params["redirect_uri"], {"code": code, "state": params["state"]})


# ---------------------------------------------------------------------------
# Token-Endpunkt
# ---------------------------------------------------------------------------


def _client_credentials() -> tuple[str | None, str | None]:
    auth = request.authorization
    if auth is not None and auth.type == "basic":
        return auth.username, auth.password
    return request.form.get("client_id"), request.form.get("client_secret")


def _authenticate_client(db_session) -> OAuthClient | None:
    client_id, client_secret = _client_credentials()
    client = _load_client(db_session, client_id)
    if client is None:
        return None
    if client.token_endpoint_auth_method == "none":
        return client
    if not client_secret or not client.client_secret_hash:
        return None
    if not constant_time_equals(hash_api_token(client_secret), client.client_secret_hash):
        return None
    return client


def _pkce_matches(code_verifier: str, code_challenge: str) -> bool:
    if not 43 <= len(code_verifier) <= 128:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii", errors="replace")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return constant_time_equals(expected, code_challenge)


def _access_seconds() -> int:
    return int(current_app.config.get("OAUTH_ACCESS_TOKEN_SECONDS") or 3600)


def _refresh_seconds() -> int:
    return int(current_app.config.get("OAUTH_REFRESH_TOKEN_DAYS") or 30) * 86400


def _issue_tokens(grant: OAuthGrant) -> dict[str, Any]:
    """Setzt neue Access-/Refresh-Tokens am Grant und liefert die Token-Antwort."""
    now = _utcnow()
    access_token = f"{ACCESS_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    refresh_token = f"{REFRESH_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    grant.access_token_hash = hash_api_token(access_token)
    grant.access_token_expires_at = now + timedelta(seconds=_access_seconds())
    grant.refresh_token_hash = hash_api_token(refresh_token)
    grant.refresh_token_expires_at = now + timedelta(seconds=_refresh_seconds())
    grant.last_used_at = now
    body: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": _access_seconds(),
        "refresh_token": refresh_token,
    }
    if grant.scope:
        body["scope"] = grant.scope
    return body


@oauth_bp.post("/oauth/token")
def token():
    grant_type = request.form.get("grant_type")
    with _get_session_factory()() as db_session:
        client = _authenticate_client(db_session)
        if client is None:
            return _oauth_error("invalid_client", "Client-Authentifizierung fehlgeschlagen.", 401)

        if grant_type == "authorization_code":
            body = _exchange_code(db_session, client)
        elif grant_type == "refresh_token":
            body = _refresh(db_session, client)
        else:
            return _oauth_error("unsupported_grant_type", "grant_type nicht unterstützt.")
        if isinstance(body, tuple):
            return _oauth_error(*body)
        db_session.commit()
    return _no_store(jsonify(body))


def _exchange_code(db_session, client: OAuthClient):
    code = request.form.get("code") or ""
    code_verifier = request.form.get("code_verifier") or ""
    stored = db_session.execute(
        select(OAuthAuthorizationCode).where(
            OAuthAuthorizationCode.code_hash == hash_api_token(code)
        )
    ).scalar_one_or_none() if code else None
    now = _utcnow()
    if (
        stored is None
        or stored.client_id != client.id
        or stored.used_at is not None
        or _aware(stored.expires_at) < now
    ):
        return ("invalid_grant", "Autorisierungscode ungültig, abgelaufen oder verbraucht.")
    redirect_uri = request.form.get("redirect_uri")
    if redirect_uri and redirect_uri != stored.redirect_uri:
        return ("invalid_grant", "redirect_uri stimmt nicht überein.")
    if not _pkce_matches(code_verifier, stored.code_challenge):
        return ("invalid_grant", "PKCE-Prüfung (code_verifier) fehlgeschlagen.")
    user = db_session.get(User, stored.user_id)
    if user is None or not user.is_active:
        return ("invalid_grant", "Benutzer ist nicht aktiv.")

    stored.used_at = now
    grant = OAuthGrant(
        client_id=client.id,
        user_id=user.id,
        access_token_hash="",
        access_token_expires_at=now,
        scope=stored.scope,
        resource=stored.resource,
    )
    body = _issue_tokens(grant)
    db_session.add(grant)
    return body


def _refresh(db_session, client: OAuthClient):
    refresh_token = request.form.get("refresh_token") or ""
    grant = db_session.execute(
        select(OAuthGrant).where(OAuthGrant.refresh_token_hash == hash_api_token(refresh_token))
    ).scalar_one_or_none() if refresh_token else None
    now = _utcnow()
    if (
        grant is None
        or grant.client_id != client.id
        or grant.revoked_at is not None
        or grant.refresh_token_expires_at is None
        or _aware(grant.refresh_token_expires_at) < now
    ):
        return ("invalid_grant", "Refresh-Token ungültig, abgelaufen oder widerrufen.")
    user = db_session.get(User, grant.user_id)
    if user is None or not user.is_active:
        return ("invalid_grant", "Benutzer ist nicht aktiv.")
    return _issue_tokens(grant)


@oauth_bp.post("/oauth/revoke")
def revoke():
    """RFC 7009: Access- oder Refresh-Token widerrufen (antwortet immer mit 200)."""
    token_value = request.form.get("token") or ""
    with _get_session_factory()() as db_session:
        client = _authenticate_client(db_session)
        if client is None:
            return _oauth_error("invalid_client", "Client-Authentifizierung fehlgeschlagen.", 401)
        if token_value:
            digest = hash_api_token(token_value)
            grant = db_session.execute(
                select(OAuthGrant).where(
                    (OAuthGrant.access_token_hash == digest)
                    | (OAuthGrant.refresh_token_hash == digest)
                )
            ).scalar_one_or_none()
            if grant is not None and grant.client_id == client.id and grant.revoked_at is None:
                revoke_grant(db_session, grant, actor=client.client_name)
                db_session.commit()
    return _no_store(current_app.make_response(("", 200)))


# ---------------------------------------------------------------------------
# Gemeinsame Helfer für API, UI und Token-Prüfung
# ---------------------------------------------------------------------------


def lookup_user_by_access_token(db_session, token_value: str) -> User | None:
    """Aktiver Benutzer zu einem gültigen, nicht widerrufenen OAuth-Access-Token."""
    if not token_value.startswith(ACCESS_TOKEN_PREFIX):
        return None
    grant = db_session.execute(
        select(OAuthGrant).where(OAuthGrant.access_token_hash == hash_api_token(token_value))
    ).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None:
        return None
    if _aware(grant.access_token_expires_at) < _utcnow():
        return None
    user = db_session.get(User, grant.user_id)
    if user is None or not user.is_active:
        return None
    return user


def revoke_grant(db_session, grant: OAuthGrant, *, actor: str) -> None:
    grant.revoked_at = _utcnow()
    grant.refresh_token_hash = None
    user = db_session.get(User, grant.user_id)
    record_security_event(
        db_session,
        user=user,
        action="oauth_revoked",
        actor=actor,
        payload={"grant_id": grant.id, "client_id": grant.client.client_id},
    )


def grant_dict(grant: OAuthGrant) -> dict[str, Any]:
    def _iso(value: datetime | None) -> str | None:
        value = _aware(value)
        return value.isoformat() if value is not None else None

    now = _utcnow()
    refresh_expires = _aware(grant.refresh_token_expires_at)
    active = grant.revoked_at is None and (refresh_expires is None or refresh_expires > now)
    return {
        "id": grant.id,
        "client_id": grant.client.client_id,
        "client_name": grant.client.client_name,
        "redirect_hosts": sorted(
            {urlsplit(uri).hostname or "" for uri in grant.client.redirect_uris}
        ),
        "user_id": grant.user_id,
        "username": grant.user.username,
        "scope": grant.scope,
        "created_at": _iso(grant.created_at),
        "last_used_at": _iso(grant.last_used_at),
        "expires_at": _iso(refresh_expires),
        "revoked_at": _iso(grant.revoked_at),
        "active": active,
    }


def list_grants(db_session, *, user_id: int | None, include_inactive: bool = False):
    """Grants eines Benutzers (``user_id``) oder aller Benutzer (``None``), neueste zuerst."""
    query = select(OAuthGrant).order_by(OAuthGrant.created_at.desc(), OAuthGrant.id.desc())
    if user_id is not None:
        query = query.where(OAuthGrant.user_id == user_id)
    grants = db_session.execute(query).scalars().all()
    rows = [grant_dict(grant) for grant in grants]
    if not include_inactive:
        rows = [row for row in rows if row["active"]]
    return rows
