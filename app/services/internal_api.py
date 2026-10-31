"""In-Process-Aufrufe der eigenen REST-API mit dem Auth-Kontext des Aufrufers.

Der :class:`InProcessApiClient` ist ein Duck-Type-Ersatz für den
``HttpApiClient`` des MCP-Servers (:mod:`app.services.mcp_server`): Statt über
HTTP ruft er die REST-API über den Flask-Testclient auf. Der Auth-Kontext
(API-Benutzer bzw. globaler Zugriff) wird über den WSGI-environ-Eintrag
``openbuchhaltung.internal_api`` an :func:`app.auth.require_api_token`
übergeben. Externe Requests können nur ``HTTP_*``-Schlüssel setzen und diesen
Eintrag daher nicht fälschen. Damit gelten für jeden In-Process-Aufruf exakt die
Tenant-/Rollen-Regeln der REST-API — genutzt vom KI-Chat, von der
JSON-RPC-Bridge ``POST /api/v1/mcp/call`` und von den Download-Links.

Die inneren Requests laufen unter der Basis-URL des äußeren Requests, damit
Links in den Antworten (``download_url``) auf die Adresse zeigen, unter der der
Benutzer die App erreicht — und nicht auf ``http://localhost``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from flask import has_request_context, request

from app.services.mcp_server import ApiResponse

INTERNAL_API_ENVIRON_KEY = "openbuchhaltung.internal_api"

# Basis-URL für Hintergrund-Threads ohne Request-Kontext (gestreamter KI-Chat).
_BASE_URL: ContextVar[str | None] = ContextVar(
    "openbuchhaltung_internal_api_base_url", default=None
)


@contextmanager
def internal_api_base_url(base_url: str | None) -> Iterator[None]:
    """Setzt die Basis-URL der In-Process-Aufrufe im aktuellen Kontext."""
    reset_token = _BASE_URL.set(base_url)
    try:
        yield
    finally:
        _BASE_URL.reset(reset_token)


def _default_base_url() -> str | None:
    if has_request_context():
        return request.url_root
    return _BASE_URL.get()


class InProcessApiClient:
    """Ruft die eigene REST-API in-process mit festem Auth-Kontext auf."""

    def __init__(
        self,
        app,
        *,
        api_user: dict | None,
        global_access: bool,
        base_url: str | None = None,
        channel: str | None = None,
    ) -> None:
        self._app = app
        self._base_url = base_url or _default_base_url()
        self._environ = {
            INTERNAL_API_ENVIRON_KEY: {
                "user": dict(api_user) if api_user else None,
                "global_access": global_access,
                # ``mcp`` für die JSON-RPC-Bridge: gleiche Regeln wie der MCP-Server.
                "channel": channel,
            }
        }

    def open_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        buffered: bool = True,
    ):
        """Roher Test-Client-Response; ungepuffert muss der Aufrufer ``close()`` rufen."""
        client = self._app.test_client()
        kwargs: dict[str, Any] = {
            "method": method,
            "query_string": params or None,
            "environ_base": dict(self._environ),
            "buffered": buffered,
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if json_body is not None:
            kwargs["json"] = json_body
        return client.open(f"/api/v1{path}", **kwargs)

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ApiResponse:
        response = self.open_raw(method, path, params=params, json_body=json_body)
        text = response.get_data(as_text=True)
        content_type = response.headers.get("Content-Type", "")
        parsed = None
        if "application/json" in content_type:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        return ApiResponse(
            status=response.status_code, text=text, content_type=content_type, json=parsed
        )
