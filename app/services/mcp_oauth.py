"""OAuth 2.1 für externe MCP-Server: Obi als OAuth-Client.

Gegenstück zum eigenen Autorisierungsserver (:mod:`app.oauth`), umgesetzt nach der
MCP-Autorisierungsspezifikation (2025-06-18):

* Discovery: ``WWW-Authenticate: Bearer resource_metadata=…`` aus der 401-Antwort des
  MCP-Servers bzw. ``/.well-known/oauth-protected-resource`` (RFC 9728) nennt den
  Autorisierungsserver, dessen Metadaten aus RFC 8414 oder OpenID Connect Discovery
  kommen. Ältere Server (2025-03-26) ohne Resource-Metadaten sind selbst der
  Autorisierungsserver (Metadaten oder ``/authorize``, ``/token``, ``/register``).
  Der Administrator kann Autorisierungs- und Token-URL auch fest eintragen.
* Client: eingetragene Client-ID (optional mit Secret) oder Dynamic Client
  Registration (RFC 7591) als öffentlicher Client.
* Authorization-Code-Flow mit PKCE (S256) und ``resource`` (RFC 8707), Refresh-Token
  mit Rotation. Endpunkte, an die Codes, Tokens oder Secrets gehen, müssen HTTPS
  verwenden (HTTP nur auf Loopback-Adressen).
* Prüfungen gegen untergeschobene Metadaten: Die Resource-Metadaten müssen zur URL des
  MCP-Servers gehören (RFC 9728, Abschnitt 3.3), ihr Verweis aus dem 401 vom selben
  Ursprung stammen, und der Aussteller der Server-Metadaten muss dem angefragten
  entsprechen (RFC 8414, Abschnitt 3.3). Den ``iss``-Parameter des Rücksprungs
  (RFC 9207) prüft :mod:`app.services.mcp_connections`.

Nur stdlib (urllib), wie die übrigen ausgehenden Anbindungen; Weiterleitungen werden
nicht verfolgt und gesperrte Adressen nicht angefragt (:func:`mcp_client.open_url`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from app.services.mcp_client import McpClientError, open_url

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 15
MAX_METADATA_BYTES = 256 * 1024
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
AUTH_METHOD_NONE = "none"
AUTH_METHOD_POST = "client_secret_post"
AUTH_METHOD_BASIC = "client_secret_basic"
_CHALLENGE_PARAM = re.compile(r'([A-Za-z_]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


class McpOAuthError(Exception):
    """OAuth-Anmeldung bei einem MCP-Server ist nicht möglich oder fehlgeschlagen.

    ``code`` ist der OAuth-Fehlercode des Autorisierungsservers (z. B. ``invalid_grant``).
    """

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AuthorizationServer:
    """Endpunkte und Angaben, die für die Anmeldung bei einem MCP-Server nötig sind."""

    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    issuer: str | None = None
    resource: str | None = None
    scopes: tuple[str, ...] = ()
    token_auth_methods: tuple[str, ...] = ()
    # RFC 9207: Der Server schickt ``iss`` im Rücksprung mit (dann ist es Pflicht).
    iss_parameter_supported: bool = False


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def canonical_resource(url: str) -> str:
    """Kanonische Resource-URI des MCP-Servers (RFC 8707): ohne Fragment, Host klein."""
    parts = urlsplit(url.strip())
    path = "" if parts.path == "/" else parts.path
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def is_loopback_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS


def checked_endpoint(url: object, label: str) -> str:
    """Prüft einen Endpunkt für Codes/Tokens: HTTPS (HTTP nur Loopback), ohne Fragment."""
    if not isinstance(url, str) or not url.strip():
        raise McpOAuthError(f"{label} fehlt.")
    url = url.strip()
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise McpOAuthError(f"{label} ist keine gültige URL.")
    if parts.scheme == "https" or (parts.scheme == "http" and is_loopback_url(url)):
        return url
    raise McpOAuthError(f"{label} muss HTTPS verwenden (HTTP nur für localhost).")


def protected_resource_metadata_urls(server_url: str) -> list[str]:
    """Kandidaten für die Protected Resource Metadata (RFC 9728, Abschnitt 3.1)."""
    parts = urlsplit(server_url)
    base = _origin(server_url)
    path = parts.path.rstrip("/")
    urls = []
    if path:
        urls.append(f"{base}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{base}/.well-known/oauth-protected-resource")
    return urls


def authorization_server_metadata_urls(issuer: str) -> list[str]:
    """Kandidaten für die Metadaten des Autorisierungsservers (RFC 8414, OIDC)."""
    parts = urlsplit(issuer)
    base = _origin(issuer)
    path = parts.path.rstrip("/")
    if path:
        return [
            f"{base}/.well-known/oauth-authorization-server{path}",
            f"{base}/.well-known/openid-configuration{path}",
            f"{base}{path}/.well-known/openid-configuration",
        ]
    return [
        f"{base}/.well-known/oauth-authorization-server",
        f"{base}/.well-known/openid-configuration",
    ]


def parse_www_authenticate(header: str | None) -> dict[str, str]:
    """Parameter der Bearer-Challenge (``resource_metadata``, ``scope``, ``error``)."""
    if not header:
        return {}
    params: dict[str, str] = {}
    for match in _CHALLENGE_PARAM.finditer(header):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        params.setdefault(match.group(1).lower(), value.replace('\\"', '"'))
    return params


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_json(url: str) -> dict[str, Any] | None:
    """GET auf ein Metadaten-Dokument; ``None`` bei Fehlern oder fehlendem JSON-Objekt."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with open_url(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_METADATA_BYTES + 1)
    except (McpClientError, HTTPError, URLError, OSError, HTTPException, ValueError):
        return None
    if len(body) > MAX_METADATA_BYTES:
        return None
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _post(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any] | None]:
    request = Request(url, data=data, headers=headers, method="POST")
    try:
        with open_url(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read(MAX_METADATA_BYTES + 1)
    except McpClientError as exc:
        raise McpOAuthError(str(exc)) from exc
    except HTTPError as exc:
        status = exc.code
        try:
            body = exc.read(MAX_METADATA_BYTES + 1)
        except (OSError, HTTPException, ValueError):
            body = b""
    except (URLError, OSError, HTTPException, ValueError) as exc:
        raise McpOAuthError("Der Autorisierungsserver ist nicht erreichbar.") from exc
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace")) if body else None
    except ValueError:
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _valid_metadata(body: object) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("authorization_endpoint"), str)
        and isinstance(body.get("token_endpoint"), str)
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def resource_matches(resource: str, server_url: str) -> bool:
    """RFC 9728, Abschnitt 3.3: Die Metadaten gehören zu diesem MCP-Server – dieselbe URI
    oder ein übergeordneter Pfad am selben Ursprung (manche Server melden nur diesen)."""
    expected = urlsplit(canonical_resource(server_url))
    actual = urlsplit(canonical_resource(resource))
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        return False
    if actual.query and actual.query != expected.query:
        return False
    prefix = actual.path.rstrip("/")
    path = expected.path.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def same_issuer(left: str, right: str) -> bool:
    return left.strip().rstrip("/") == right.strip().rstrip("/")


def _resource_metadata(
    server_url: str, header_url: str | None, fetch: Callable[[str], dict[str, Any] | None]
) -> dict[str, Any] | None:
    candidates = []
    if header_url and _origin(header_url) == _origin(server_url):
        candidates.append(header_url)
    elif header_url:
        # Ein Verweis auf fremde Hosts würde Abrufe an beliebige Adressen auslösen.
        logger.warning(
            "MCP-Server %s verweist auf Resource-Metadaten eines anderen Ursprungs; ignoriert.",
            _origin(server_url),
        )
    candidates.extend(protected_resource_metadata_urls(server_url))
    for url in candidates:
        body = fetch(url)
        if not isinstance(body, dict) or not _strings(body.get("authorization_servers")):
            continue
        declared = body.get("resource")
        if isinstance(declared, str) and declared.strip():
            if not resource_matches(declared.strip(), server_url):
                raise McpOAuthError("Die Resource-Metadaten gehören nicht zu diesem MCP-Server.")
        return body
    return None


def _server_metadata(
    issuer: str, urls: list[str], fetch: Callable[[str], dict[str, Any] | None]
) -> dict[str, Any] | None:
    for url in urls:
        body = fetch(url)
        if not _valid_metadata(body):
            continue
        declared = body.get("issuer")
        if isinstance(declared, str) and declared and not same_issuer(declared, issuer):
            raise McpOAuthError(
                "Die Metadaten des Autorisierungsservers nennen einen anderen Aussteller."
            )
        return body
    return None


def discover(
    server_url: str,
    *,
    www_authenticate: str | None = None,
    authorization_endpoint: str | None = None,
    token_endpoint: str | None = None,
    fetch_json: Callable[[str], dict[str, Any] | None] | None = None,
) -> AuthorizationServer:
    """Ermittelt den Autorisierungsserver eines MCP-Servers.

    ``www_authenticate`` ist der Header einer 401-Antwort des MCP-Servers (falls
    bekannt); fest eingetragene Endpunkte haben Vorrang vor den Metadaten.
    """
    fetch = fetch_json or _get_json
    challenge = parse_www_authenticate(www_authenticate)
    resource = canonical_resource(server_url)
    scopes = tuple(challenge.get("scope", "").split())

    resource_metadata = _resource_metadata(
        server_url, challenge.get("resource_metadata"), fetch
    )
    issuers: tuple[str, ...] = ()
    if resource_metadata is not None:
        if isinstance(resource_metadata.get("resource"), str) and resource_metadata["resource"]:
            resource = resource_metadata["resource"].strip()
        if not scopes:
            scopes = _strings(resource_metadata.get("scopes_supported"))
        issuers = _strings(resource_metadata.get("authorization_servers"))

    metadata: dict[str, Any] = {}
    if not (authorization_endpoint and token_endpoint):
        if issuers:
            metadata = _server_metadata(
                issuers[0], authorization_server_metadata_urls(issuers[0]), fetch
            ) or {}
            if not metadata:
                raise McpOAuthError(
                    "Die Metadaten des Autorisierungsservers wurden nicht gefunden."
                )
        else:
            # Ältere MCP-Server (2025-03-26): der MCP-Server ist selbst der
            # Autorisierungsserver, Metadaten oder Standardpfade an seinem Ursprung.
            base = _origin(server_url)
            metadata = _server_metadata(
                base, [f"{base}/.well-known/oauth-authorization-server"], fetch
            ) or {
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
            }

    methods = metadata.get("code_challenge_methods_supported")
    if isinstance(methods, list) and methods and "S256" not in methods:
        raise McpOAuthError("Der Autorisierungsserver unterstützt kein PKCE mit S256.")
    registration = metadata.get("registration_endpoint")
    issuer = metadata.get("issuer")
    return AuthorizationServer(
        authorization_endpoint=checked_endpoint(
            authorization_endpoint or metadata.get("authorization_endpoint"),
            "Die Autorisierungs-URL",
        ),
        token_endpoint=checked_endpoint(
            token_endpoint or metadata.get("token_endpoint"), "Die Token-URL"
        ),
        registration_endpoint=(
            checked_endpoint(registration, "Die Registrierungs-URL")
            if isinstance(registration, str) and registration
            else None
        ),
        issuer=issuer if isinstance(issuer, str) and issuer else (issuers[0] if issuers else None),
        resource=resource,
        scopes=scopes,
        token_auth_methods=_strings(metadata.get("token_endpoint_auth_methods_supported")),
        iss_parameter_supported=metadata.get("authorization_response_iss_parameter_supported")
        is True,
    )


# ---------------------------------------------------------------------------
# Client und Authorization-Code-Flow
# ---------------------------------------------------------------------------


def register_client(
    server: AuthorizationServer,
    *,
    redirect_uri: str,
    client_name: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """Dynamic Client Registration (RFC 7591) als öffentlicher Client mit PKCE."""
    if not server.registration_endpoint:
        raise McpOAuthError("Der Autorisierungsserver bietet keine Client-Registrierung an.")
    payload: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": AUTH_METHOD_NONE,
    }
    if scope:
        payload["scope"] = scope
    status, body = _post(
        server.registration_endpoint,
        json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json", "Accept": "application/json"},
    )
    client_id = body.get("client_id") if body else None
    if status not in {200, 201} or not isinstance(client_id, str) or not client_id:
        logger.warning(
            "Client-Registrierung bei %s fehlgeschlagen: HTTP %s %s",
            server.registration_endpoint,
            status,
            str(body)[:300],
        )
        raise McpOAuthError(
            "Die Client-Registrierung beim Autorisierungsserver ist fehlgeschlagen."
        )
    client_secret = body.get("client_secret")
    client_secret = client_secret if isinstance(client_secret, str) and client_secret else None
    auth_method = body.get("token_endpoint_auth_method")
    if auth_method not in {AUTH_METHOD_NONE, AUTH_METHOD_POST, AUTH_METHOD_BASIC}:
        auth_method = AUTH_METHOD_BASIC if client_secret else AUTH_METHOD_NONE
    return {"client_id": client_id, "client_secret": client_secret, "auth_method": auth_method}


def client_auth_method(server: AuthorizationServer, client_secret: str | None) -> str:
    """Client-Authentifizierung für eine eingetragene Client-ID (Basic ist RFC-Standard)."""
    if not client_secret:
        return AUTH_METHOD_NONE
    methods = server.token_auth_methods
    if methods and AUTH_METHOD_BASIC not in methods and AUTH_METHOD_POST in methods:
        return AUTH_METHOD_POST
    return AUTH_METHOD_BASIC


def pkce_pair() -> tuple[str, str]:
    """Code-Verifier und S256-Challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(
    server: AuthorizationServer,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    if server.resource:
        params["resource"] = server.resource
    separator = "&" if "?" in server.authorization_endpoint else "?"
    return f"{server.authorization_endpoint}{separator}{urlencode(params)}"


def _token_set(body: dict[str, Any]) -> dict[str, Any]:
    try:
        expires_in = int(body.get("expires_in"))
    except (TypeError, ValueError):
        expires_in = 0
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = None
    scope = body.get("scope")
    return {
        "access_token": body["access_token"],
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + expires_in if expires_in > 0 else None,
        "scope": scope if isinstance(scope, str) else None,
    }


def _token_request(
    token_endpoint: str,
    form: dict[str, str | None],
    *,
    client_id: str,
    client_secret: str | None,
    auth_method: str,
) -> dict[str, Any]:
    data = {key: value for key, value in form.items() if value}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    if auth_method == AUTH_METHOD_BASIC and client_secret:
        pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
    else:
        data["client_id"] = client_id
        if auth_method == AUTH_METHOD_POST and client_secret:
            data["client_secret"] = client_secret
    status, body = _post(
        checked_endpoint(token_endpoint, "Die Token-URL"), urlencode(data).encode(), headers
    )
    if body and isinstance(body.get("error"), str):
        description = str(body.get("error_description") or body["error"])[:200]
        raise McpOAuthError(f"Der Autorisierungsserver meldet: {description}", code=body["error"])
    if status != 200 or not body or not isinstance(body.get("access_token"), str):
        raise McpOAuthError("Der Autorisierungsserver lieferte kein Access-Token.")
    return _token_set(body)


def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None = None,
    auth_method: str = AUTH_METHOD_NONE,
    resource: str | None = None,
) -> dict[str, Any]:
    """Tauscht den Autorisierungscode gegen Tokens (``access_token``, ``refresh_token``,
    ``expires_at`` als Unix-Zeit, ``scope``)."""
    return _token_request(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "resource": resource,
        },
        client_id=client_id,
        client_secret=client_secret,
        auth_method=auth_method,
    )


def refresh_tokens(
    token_endpoint: str,
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str | None = None,
    auth_method: str = AUTH_METHOD_NONE,
    resource: str | None = None,
) -> dict[str, Any]:
    """Erneuert das Access-Token; ohne neues Refresh-Token bleibt das alte gültig."""
    tokens = _token_request(
        token_endpoint,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "resource": resource},
        client_id=client_id,
        client_secret=client_secret,
        auth_method=auth_method,
    )
    tokens["refresh_token"] = tokens["refresh_token"] or refresh_token
    return tokens
