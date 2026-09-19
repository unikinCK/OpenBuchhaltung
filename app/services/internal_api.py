"""In-Process-Aufrufe der eigenen REST-API mit dem Auth-Kontext des Aufrufers.

Der :class:`InProcessApiClient` ist ein Duck-Type-Ersatz für den
``HttpApiClient`` des MCP-Servers (:mod:`app.services.mcp_server`): Statt über
HTTP ruft er die REST-API über den Flask-Testclient auf. Der Auth-Kontext
(API-Benutzer bzw. globaler Zugriff) wird über den WSGI-environ-Eintrag
``openbuchhaltung.internal_api`` an :func:`app.auth.require_api_token`
übergeben. Externe Requests können nur ``HTTP_*``-Schlüssel setzen und diesen
Eintrag daher nicht fälschen. Damit gelten für jeden In-Process-Aufruf exakt die
Tenant-/Rollen-Regeln der REST-API — genutzt vom KI-Chat und von der
JSON-RPC-Bridge ``POST /api/v1/mcp/call``.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.mcp_server import ApiResponse

INTERNAL_API_ENVIRON_KEY = "openbuchhaltung.internal_api"


class InProcessApiClient:
    """Ruft die eigene REST-API in-process mit festem Auth-Kontext auf."""

    def __init__(self, app, *, api_user: dict | None, global_access: bool) -> None:
        self._app = app
        self._environ = {
            INTERNAL_API_ENVIRON_KEY: {
                "user": dict(api_user) if api_user else None,
                "global_access": global_access,
            }
        }

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ApiResponse:
        client = self._app.test_client()
        kwargs: dict[str, Any] = {
            "method": method,
            "query_string": params or None,
            "environ_base": dict(self._environ),
        }
        if json_body is not None:
            kwargs["json"] = json_body
        response = client.open(f"/api/v1{path}", **kwargs)
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
