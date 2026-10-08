"""Streamable-HTTP-Transport für den OpenBuchhaltung-MCP-Server.

Ergänzt den stdio-Transport aus :mod:`app.services.mcp_server` um einen HTTP-Endpunkt
gemäß MCP-"Streamable HTTP": Ein einzelner Pfad (Standard ``/mcp``) nimmt per ``POST``
JSON-RPC-Nachrichten entgegen und antwortet – je nach ``Accept``-Header – entweder mit
``application/json`` oder als ``text/event-stream`` (SSE). Bewusst ohne externe
Abhängigkeiten (nur stdlib), analog zum stdio-Transport.

Start (die OpenBuchhaltung-App muss unter OPENBUCHHALTUNG_API_URL erreichbar sein)::

    OPENBUCHHALTUNG_API_URL=http://localhost:8000/api/v1 \\
    OPENBUCHHALTUNG_API_TOKEN=obk_... \\
    MCP_HTTP_HOST=127.0.0.1 MCP_HTTP_PORT=8080 \\
    python -m app.services.mcp_http

Authentifizierung: ``MCP_HTTP_AUTH_TOKEN`` öffnet den Endpunkt; die Tools laufen dann
mit dem Server-Token ``OPENBUCHHALTUNG_API_TOKEN``. Zusätzlich (``MCP_HTTP_ALLOW_USER_TOKENS``,
Default an) akzeptiert der Endpunkt Benutzer-API-Tokens: Sie werden gegen
``GET /users/me`` geprüft und pro Request an die REST-API durchgereicht, sodass Rolle,
Mandanten-Scope und Protokoll des jeweiligen Benutzers gelten. Dazu zählen auch
OAuth-Access-Tokens der App (ChatGPT-/Claude-Connectoren): Ein 401 trägt dann
``WWW-Authenticate: Bearer resource_metadata=…`` (öffentliche Basis-URL aus
``MCP_PUBLIC_URL`` bzw. X-Forwarded-Proto/Host), über das Clients den Login finden.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.services.mcp_server import (
    MCPServer,
    MCPTransportError,
    api_token_override,
    build_server_from_env,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_PATH = "/mcp"
# Obergrenze für Request-Bodies (Speicher-DoS über Content-Length); Beleg-Uploads
# per MCP sind Base64 und bleiben mit 16 MiB deutlich unter dem Upload-Limit.
DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
# Wie lange ein geprüfter Benutzer-Token ohne erneute API-Prüfung gilt. Die REST-API
# prüft den durchgereichten Token ohnehin bei jedem Tool-Aufruf; der Cache spart nur
# die Vorab-Prüfung (gesperrte Benutzer scheitern also sofort am API-Aufruf).
DEFAULT_USER_TOKEN_CACHE_SECONDS = 60.0
_USER_TOKEN_CACHE_MAX_ENTRIES = 1024


@dataclass(slots=True)
class HttpResult:
    status: int
    content_type: str | None
    body: str = ""

    @property
    def body_bytes(self) -> bytes:
        return self.body.encode("utf-8")


def _json_rpc_error(code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}


def _sse(responses: list[dict[str, Any]]) -> str:
    """Kodiert JSON-RPC-Antworten als Server-Sent-Events."""
    chunks = []
    for response in responses:
        data = json.dumps(response, ensure_ascii=False)
        chunks.append(f"event: message\ndata: {data}\n\n")
    return "".join(chunks)


def accepts_event_stream(accept_header: str | None) -> bool:
    return bool(accept_header) and "text/event-stream" in accept_header


def accepts_json(accept_header: str | None) -> bool:
    """Ob der Client JSON akzeptiert (fehlender Accept-Header = ja)."""
    if not accept_header:
        return True
    return "application/json" in accept_header or "*/*" in accept_header


def process_post(server: MCPServer, raw_body: str, accept_header: str | None) -> HttpResult:
    """Verarbeitet einen POST-Body (einzelne Nachricht oder Batch) transportneutral."""
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError:
        return HttpResult(
            status=400,
            content_type="application/json",
            body=json.dumps(_json_rpc_error(-32700, "Parse error")),
        )

    messages = parsed if isinstance(parsed, list) else [parsed]
    if not messages:
        return HttpResult(status=400, content_type="application/json",
                          body=json.dumps(_json_rpc_error(-32600, "Invalid Request")))

    responses: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            responses.append(_json_rpc_error(-32600, "Invalid Request"))
            continue
        response = server.handle(message)
        if response is not None:
            responses.append(response)

    # Nur Notifications/Responses ohne Rückgabe -> 202 Accepted ohne Body.
    if not responses:
        return HttpResult(status=202, content_type=None)

    # Für eine einzelne Request/Response-Runde bevorzugen wir JSON. SSE nur, wenn der
    # Client ausschließlich text/event-stream akzeptiert. MCP-Clients (z. B. Claude
    # Desktop) senden "application/json, text/event-stream" und erwarten dann JSON —
    # eine SSE-Antwort führt dort zu einem Verbindungsfehler.
    if accepts_event_stream(accept_header) and not accepts_json(accept_header):
        return HttpResult(status=200, content_type="text/event-stream", body=_sse(responses))

    payload = responses if isinstance(parsed, list) else responses[0]
    return HttpResult(
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


def origin_allowed(origin: str | None, allowed_origins: frozenset[str]) -> bool:
    """DNS-Rebinding-Schutz: Browser-Origins müssen explizit erlaubt sein.

    Clients ohne ``Origin``-Header (z. B. Claude Desktop, CLI) sind immer zulässig.
    Steht ``*`` in der Allowlist, sind alle Origins erlaubt — sinnvoll, wenn der
    Server bereits hinter einem vertrauenswürdigen Proxy (Tailscale Serve, Caddy)
    mit eigenem Zugriffsschutz steht.
    """
    if not origin:
        return True
    if "*" in allowed_origins:
        return True
    return origin in allowed_origins


def authorization_allowed(authorization: str | None, auth_token: str | None) -> bool:
    """Validate the optional inbound bearer token for Streamable HTTP."""
    if not auth_token:
        return True
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied_token = authorization.removeprefix("Bearer ").strip()
    # Byte-Vergleich: compare_digest wirft bei Nicht-ASCII-Strings einen TypeError.
    return bool(supplied_token) and secrets.compare_digest(
        supplied_token.encode("utf-8"), auth_token.encode("utf-8")
    )


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip() or None


class UserTokenValidator:
    """Prüft Benutzer-API-Tokens gegen ``GET /users/me`` (mit kurzem Positiv-Cache)."""

    def __init__(
        self, server: MCPServer, cache_seconds: float = DEFAULT_USER_TOKEN_CACHE_SECONDS
    ) -> None:
        self._server = server
        self._cache_seconds = cache_seconds
        self._valid_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_valid(self, token: str) -> bool:
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        with self._lock:
            if self._valid_until.get(key, 0.0) > now:
                return True
        try:
            with api_token_override(token):
                response = self._server.http.call("GET", "/users/me")
        except MCPTransportError:
            return False
        with self._lock:
            if response.status != 200:
                self._valid_until.pop(key, None)
                return False
            if len(self._valid_until) >= _USER_TOKEN_CACHE_MAX_ENTRIES:
                self._valid_until = {k: v for k, v in self._valid_until.items() if v > now}
            self._valid_until[key] = now + self._cache_seconds
        return True


def resolve_authorization(
    authorization: str | None,
    auth_token: str | None,
    user_tokens: UserTokenValidator | None,
) -> tuple[bool, str | None]:
    """Entscheidet über den Zugang und liefert ggf. den durchzureichenden API-Token.

    Rückgabe ``(erlaubt, api_token)``: ``api_token`` ist ``None`` beim
    ``MCP_HTTP_AUTH_TOKEN`` (Server-Token gilt) und der Benutzer-Token, wenn der
    Client sich mit einem gültigen Benutzer-API-Token ausweist.
    """
    if authorization_allowed(authorization, auth_token) and auth_token:
        return True, None
    supplied = _bearer_token(authorization)
    if supplied and user_tokens is not None and user_tokens.is_valid(supplied):
        return True, supplied
    if not auth_token:
        # Ohne Eingangstoken (nur Loopback zulässig) bleibt der Endpunkt offen.
        return True, None
    return False, None


def _make_handler(
    server: MCPServer,
    path: str,
    allowed_origins: frozenset[str],
    auth_token: str | None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    user_tokens: UserTokenValidator | None = None,
    public_url: str | None = None,
):
    class MCPRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "OpenBuchhaltungMCP/1.0"

        def _send(self, result: HttpResult, extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(result.status)
            if result.content_type:
                self.send_header("Content-Type", result.content_type)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            body = result.body_bytes
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _reject(
            self, status: int, message: str, extra_headers: dict[str, str] | None = None
        ) -> None:
            self._send(
                HttpResult(
                    status=status,
                    content_type="application/json",
                    body=json.dumps({"error": message}),
                ),
                extra_headers,
            )

        def _unauthorized(self) -> None:
            # MCP-Autorisierung: Clients (ChatGPT, Claude) finden über resource_metadata
            # den OAuth-Server der App und starten den Login-Flow.
            headers = None
            if user_tokens is not None:
                challenge = f'Bearer resource_metadata="{self._resource_metadata_url()}"'
                if _bearer_token(self.headers.get("Authorization")):
                    challenge += ', error="invalid_token"'
                headers = {"WWW-Authenticate": challenge}
            self._reject(401, "Unauthorized.", headers)

        def _resource_metadata_url(self) -> str:
            base = public_url
            if not base:
                proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
                host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "")
                base = f"{proto}://{host.split(',')[0].strip()}"
            return f"{base.rstrip('/')}/.well-known/oauth-protected-resource{path}"

        def do_POST(self) -> None:  # noqa: N802 (stdlib-Namenskonvention)
            if self.path.split("?", 1)[0] != path:
                self._reject(404, "Not found.")
                return
            allowed, api_token = resolve_authorization(
                self.headers.get("Authorization"), auth_token, user_tokens
            )
            if not allowed:
                self._unauthorized()
                return
            if not origin_allowed(self.headers.get("Origin"), allowed_origins):
                self._reject(403, "Origin not allowed.")
                return

            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                self._reject(400, "Invalid Content-Length.")
                return
            if length > max_body_bytes:
                # Body nicht lesen; Verbindung schließen, damit kein Rest-Body als
                # nächster Request interpretiert wird.
                self.close_connection = True
                self._reject(413, "Request body too large.")
                return
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            with api_token_override(api_token):
                result = process_post(server, raw_body, self.headers.get("Accept"))
            self._send(result)

        def do_GET(self) -> None:  # noqa: N802
            # Unbekannte Pfade (u. a. OAuth-Discovery unter /.well-known/…) sauber mit
            # 404 beantworten. So erkennt ein MCP-Client, dass der Server keinen
            # Autorisierungsdienst anbietet, und verbindet ohne OAuth.
            if self.path.split("?", 1)[0] != path:
                self._reject(404, "Not found.")
                return
            # /mcp selbst bietet keinen server-initiierten SSE-Stream (GET) an.
            self._reject(405, "Method not allowed.")

        def log_message(self, *args: Any) -> None:  # Stille Standardausgabe.
            del args

    return MCPRequestHandler


def make_server(
    server: MCPServer,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    allowed_origins: frozenset[str] = frozenset(),
    auth_token: str | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    allow_user_tokens: bool = True,
    public_url: str | None = None,
) -> ThreadingHTTPServer:
    if host not in LOOPBACK_HOSTS and not auth_token:
        raise RuntimeError(
            "MCP_HTTP_AUTH_TOKEN muss gesetzt sein, wenn MCP_HTTP_HOST nicht auf Loopback bindet."
        )
    user_tokens = UserTokenValidator(server) if allow_user_tokens else None
    handler = _make_handler(
        server, path, allowed_origins, auth_token, max_body_bytes, user_tokens, public_url
    )
    return ThreadingHTTPServer((host, port), handler)


def _allowed_origins_from_env(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def main() -> None:
    server = build_server_from_env()
    host = os.environ.get("MCP_HTTP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("MCP_HTTP_PORT", str(DEFAULT_PORT)))
    path = os.environ.get("MCP_HTTP_PATH", DEFAULT_PATH)
    allowed_origins = _allowed_origins_from_env(os.environ.get("MCP_HTTP_ALLOWED_ORIGINS"))
    auth_token = (os.environ.get("MCP_HTTP_AUTH_TOKEN") or "").strip() or None
    max_body_bytes = int(os.environ.get("MCP_HTTP_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))
    allow_user_tokens_raw = os.environ.get("MCP_HTTP_ALLOW_USER_TOKENS") or "1"
    allow_user_tokens = allow_user_tokens_raw.strip().lower() not in {"0", "false", "no", "off"}

    httpd = make_server(
        server,
        host,
        port,
        path,
        allowed_origins,
        auth_token,
        max_body_bytes,
        allow_user_tokens=allow_user_tokens,
        public_url=(os.environ.get("MCP_PUBLIC_URL") or "").strip() or None,
    )
    print(f"OpenBuchhaltung MCP-Server (Streamable HTTP) läuft auf http://{host}:{port}{path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
