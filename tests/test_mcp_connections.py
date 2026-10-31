"""Externe MCP-Server für Obi: Client, OAuth, Verwaltung (UI/API) und Chat-Werkzeuge.

Die Tests sprechen einen echten HTTP-Server auf 127.0.0.1 an, der einen MCP-Server
(Streamable HTTP) samt OAuth-Autorisierungsserver nachbildet.
"""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select
from test_chat import _FakePoster, _function_call_body, _message_body

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services import mcp_client, mcp_connections
from app.services.chat import UNTRUSTED_NOTICE
from app.services.mcp_client import McpAuthError, McpClientError, McpSession, McpToolResult
from app.services.mcp_connections import (
    _decrypt,
    _encrypt,
    exposed_tool_names,
    is_external_tool_name,
)
from app.services.mcp_oauth import (
    McpOAuthError,
    checked_endpoint,
    discover,
    resource_matches,
)
from app.services.mcp_server import TOOLS
from domain.models import AuditLog, Company, McpCredential, McpServer, Tenant, User

# ---------------------------------------------------------------------------
# Fake-MCP-Server mit OAuth-Autorisierungsserver
# ---------------------------------------------------------------------------

FAKE_TOOLS = [
    {
        "name": "search_documents",
        "title": "Dokumente suchen",
        "description": "Sucht Dokumente in der Ablage.",
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "move_document",
        "description": "Verschiebt ein Dokument in einen anderen Ordner.",
        "inputSchema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}, "folder": {"type": "string"}},
        },
    },
]


def _initial_state() -> dict:
    return {
        "auth_header": None,
        "required_auth": None,
        "sse": False,
        "expire_session_once": False,
        "session_counter": 0,
        "requests": [],
        "calls": [],
        "deleted_sessions": [],
        "codes": {},
        "token_requests": [],
        "token_counter": 0,
        "valid_refresh": None,
        "expires_in": 3600,
        "registrations": [],
        # Varianten für Fehlerfälle
        "forbid_tool": None,
        "expire_on_notification": False,
        "sse_slow": False,
        "token_error": None,
        "prm_resource": None,
        "as_issuer": None,
        "iss_supported": False,
    }


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):  # noqa: D401 – Testserver bleibt still
        return None

    @property
    def state(self) -> dict:
        return self.server.state

    @property
    def base(self) -> str:
        return self.server.base_url

    def _send(self, status: int, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/.well-known/oauth-protected-resource/mcp":
            return self._send(
                200,
                {
                    "resource": self.state["prm_resource"] or f"{self.base}/mcp",
                    "authorization_servers": [f"{self.base}/as"],
                    "scopes_supported": ["docs.read"],
                },
            )
        if self.path == "/.well-known/oauth-authorization-server/as":
            return self._send(
                200,
                {
                    "issuer": self.state["as_issuer"] or f"{self.base}/as",
                    "authorization_endpoint": f"{self.base}/as/authorize",
                    "token_endpoint": f"{self.base}/as/token",
                    "registration_endpoint": f"{self.base}/as/register",
                    "code_challenge_methods_supported": ["S256"],
                    "authorization_response_iss_parameter_supported": self.state[
                        "iss_supported"
                    ],
                },
            )
        return self._send(404, {"error": "not_found"})

    def do_DELETE(self):
        self.state["deleted_sessions"].append(self.headers.get("Mcp-Session-Id"))
        self._send(200)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/as/register":
            payload = json.loads(raw)
            self.state["registrations"].append(payload)
            return self._send(201, {"client_id": "client-1", **payload})
        if self.path == "/as/token":
            return self._token(parse_qs(raw.decode()))
        if self.path == "/mcp":
            return self._mcp(json.loads(raw))
        if self.path == "/redirect":
            return self._send(307, None, {"Location": f"{self.base}/mcp"})
        return self._send(404, {"error": "not_found"})

    def _issue_tokens(self):
        self.state["token_counter"] += 1
        number = self.state["token_counter"]
        self.state["valid_refresh"] = f"rt-{number}"
        self.state["required_auth"] = f"Bearer at-{number}"
        return self._send(
            200,
            {
                "access_token": f"at-{number}",
                "token_type": "Bearer",
                "expires_in": self.state["expires_in"],
                "refresh_token": f"rt-{number}",
                "scope": "docs.read",
            },
        )

    def _token(self, form: dict):
        self.state["token_requests"].append(form)
        if self.state["token_error"]:
            return self._send(400, {"error": self.state["token_error"]})
        grant = form.get("grant_type", [""])[0]
        if grant == "authorization_code":
            entry = self.state["codes"].pop(form.get("code", [""])[0], None)
            verifier = form.get("code_verifier", [""])[0].encode()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest())
            if (
                entry is None
                or entry["challenge"] != challenge.rstrip(b"=").decode()
                or entry["redirect_uri"] != form.get("redirect_uri", [""])[0]
                or form.get("client_id", [""])[0] != "client-1"
            ):
                return self._send(400, {"error": "invalid_grant"})
            return self._issue_tokens()
        if grant == "refresh_token":
            if form.get("refresh_token", [""])[0] != self.state["valid_refresh"]:
                return self._send(400, {"error": "invalid_grant"})
            return self._issue_tokens()
        return self._send(400, {"error": "unsupported_grant_type"})

    def _mcp(self, message: dict):
        state = self.state
        header = state["auth_header"]
        auth = self.headers.get(header) if header else None
        required = state["required_auth"]
        accepted = required if isinstance(required, set) else {required}
        if required is not None and auth not in accepted:
            challenge = (
                f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp",'
                ' scope="docs.read"'
            )
            return self._send(401, None, {"WWW-Authenticate": challenge})
        method = message.get("method")
        session_id = self.headers.get("Mcp-Session-Id")
        state["requests"].append(
            {
                "method": method,
                "auth": auth,
                "session": session_id,
                "protocol": self.headers.get("MCP-Protocol-Version"),
            }
        )
        if "id" not in message:
            if state["expire_on_notification"] and session_id:
                return self._send(404, {"error": "unknown session"})
            return self._send(202)
        if method != "initialize" and state["expire_session_once"] and session_id:
            state["expire_session_once"] = False
            return self._send(404, {"error": "unknown session"})
        headers = {}
        if method == "initialize":
            state["session_counter"] += 1
            headers["Mcp-Session-Id"] = f"s-{state['session_counter']}"
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-dms", "version": "1.2"},
                "instructions": "Erst suchen, dann verschieben.",
            }
        elif method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            if cursor:
                result = {"tools": FAKE_TOOLS[1:]}
            else:
                result = {"tools": FAKE_TOOLS[:1], "nextCursor": "page-2"}
        elif method == "tools/call":
            params = message.get("params") or {}
            if params.get("name") == state["forbid_tool"]:
                challenge = 'Bearer error="insufficient_scope", scope="docs.write"'
                return self._send(403, None, {"WWW-Authenticate": challenge})
            state["calls"].append(
                {"name": params.get("name"), "arguments": params.get("arguments"), "auth": auth}
            )
            result = {
                "content": [
                    {"type": "text", "text": f"Ergebnis {params.get('name')}"},
                    {"type": "resource_link", "uri": "dms://doc/42", "name": "Rechnung 42"},
                ],
                "isError": False,
            }
        else:
            return self._send(
                200,
                {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "?"}},
            )
        response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if state["sse_slow"] and method == "tools/call":
            # Lebenszeichen ohne Ende: Die Frist des Clients muss greifen.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _tick in range(30):
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
            return None
        if state["sse"]:
            progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            data = (
                f"event: message\ndata: {json.dumps(progress)}\n\n"
                f"event: message\ndata: {json.dumps(response)}\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        return self._send(200, response, headers)


@pytest.fixture
def fake_mcp():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    server.state = _initial_state()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# App mit Benutzern
# ---------------------------------------------------------------------------

USERS = (
    # (Benutzername, Rolle, mandantengebunden)
    ("admin", "Admin", False),
    ("chef", "Admin", True),
    ("anna", "Buchhalter", True),
    ("bernd", "Buchhalter", True),
    ("paula", "Pruefer", True),
)


def _token(username: str) -> str:
    return f"obk_test_{username}_0123456789"


def _auth(username: str) -> dict:
    return {"Authorization": f"Bearer {_token(username)}"}


def _create_app(tmp_path: Path, **extra):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'mcp.db'}",
            "CHAT_LLM_ENDPOINT_URL": "http://llm.test/v1/responses",
            "CHAT_LLM_MODEL": "test-model",
            **extra,
        }
    )
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="MCP Tenant")
        session.add(tenant)
        session.flush()
        company = Company(name="MCP GmbH", currency_code="EUR", tenant=tenant)
        session.add(company)
        for username, role, bound in USERS:
            session.add(
                User(
                    username=username,
                    password_hash=hash_password("geheim123"),
                    role=role,
                    tenant_id=tenant.id if bound else None,
                    api_token_hash=hash_api_token(_token(username)),
                    api_token_last4=_token(username)[-4:],
                )
            )
        session.commit()
        app.config["_TEST_COMPANY_ID"] = company.id
        app.config["_TEST_TENANT_ID"] = tenant.id
    return app


def _login(app, username: str):
    client = app.test_client()
    client.post("/auth/login", data={"username": username, "password": "geheim123"})
    return client


def _create_server(app, fake, **settings) -> int:
    payload = {
        "name": "DMS",
        "tool_prefix": "dms",
        "url": f"{fake.base_url}/mcp",
        "description": "Belegablage; Eingangsrechnungen im Ordner Eingang.",
        **settings,
    }
    response = app.test_client().post("/api/v1/mcp-servers", json=payload, headers=_auth("admin"))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["id"]


def _token_server(app, fake, *, scope: str = "global", header: str | None = None) -> int:
    settings = {"auth_type": "token", "credential_scope": scope}
    if header:
        settings["token_header"] = header
    return _create_server(app, fake, **settings)


def _credential(app, server_id: int, user_id: int | None) -> McpCredential | None:
    with app.extensions["db_session_factory"]() as session:
        query = select(McpCredential).where(McpCredential.server_id == server_id)
        query = (
            query.where(McpCredential.user_id.is_(None))
            if user_id is None
            else query.where(McpCredential.user_id == user_id)
        )
        credential = session.execute(query).scalar_one_or_none()
        if credential is not None:
            session.expunge(credential)
        return credential


def _user_id(app, username: str) -> int:
    with app.extensions["db_session_factory"]() as session:
        return session.execute(select(User.id).where(User.username == username)).scalar_one()


# ---------------------------------------------------------------------------
# MCP-Client (Streamable HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sse", [False, True])
def test_session_lists_tools_with_pagination_and_calls_them(fake_mcp, sse):
    fake_mcp.state["sse"] = sse
    session = McpSession(f"{fake_mcp.base_url}/mcp", timeout=5)

    tools = session.list_tools()
    result = session.call_tool("search_documents", {"query": "Rechnung"})
    session.close()

    assert [tool.name for tool in tools] == ["search_documents", "move_document"]
    assert tools[0].read_only is True and tools[1].read_only is False
    assert tools[0].title == "Dokumente suchen"
    assert session.server_info == {"name": "fake-dms", "version": "1.2"}
    assert result.as_text() == "Ergebnis search_documents\n[Verweis Rechnung 42: dms://doc/42]"
    methods = [entry["method"] for entry in fake_mcp.state["requests"]]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/list",
                       "tools/call"]
    # Nach initialize: Sitzungs-ID und ausgehandelte Protokollversion in jedem Request.
    assert {entry["session"] for entry in fake_mcp.state["requests"][1:]} == {"s-1"}
    assert {entry["protocol"] for entry in fake_mcp.state["requests"][1:]} == {"2025-06-18"}
    assert fake_mcp.state["deleted_sessions"] == ["s-1"]


def test_session_reinitializes_after_expired_session(fake_mcp):
    session = McpSession(f"{fake_mcp.base_url}/mcp", timeout=5)
    session.initialize()
    fake_mcp.state["expire_session_once"] = True

    result = session.call_tool("search_documents", {"query": "x"})

    assert result.is_error is False
    assert session.session_id == "s-2"
    assert fake_mcp.state["calls"][0]["name"] == "search_documents"


def test_session_reports_auth_challenge(fake_mcp):
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer richtig")
    session = McpSession(f"{fake_mcp.base_url}/mcp", headers={"Authorization": "Bearer falsch"})

    with pytest.raises(McpAuthError) as excinfo:
        session.list_tools()

    assert "resource_metadata=" in excinfo.value.www_authenticate


def test_tool_result_text_names_binary_content():
    result = McpToolResult(
        content=[
            {"type": "image", "mimeType": "image/png", "data": "AAAA"},
            {"type": "resource", "resource": {"uri": "dms://1", "text": "Inhalt"}},
            {"type": "resource", "resource": {"uri": "dms://2", "mimeType": "application/pdf"}},
        ]
    )
    assert result.as_text() == (
        "[Bild (image/png) nicht übernommen]\n[Ressource dms://1]\nInhalt\n"
        "[Ressource dms://2 (application/pdf), Binärinhalt nicht übernommen]"
    )
    assert McpToolResult(structured_content={"a": 1}).as_text() == '{"a": 1}'


# ---------------------------------------------------------------------------
# OAuth-Discovery
# ---------------------------------------------------------------------------


def test_discover_follows_resource_metadata(fake_mcp):
    challenge = (
        f'Bearer resource_metadata="{fake_mcp.base_url}/.well-known/oauth-protected-resource/mcp"'
    )
    server = discover(f"{fake_mcp.base_url}/mcp", www_authenticate=challenge)

    assert server.authorization_endpoint == f"{fake_mcp.base_url}/as/authorize"
    assert server.token_endpoint == f"{fake_mcp.base_url}/as/token"
    assert server.registration_endpoint == f"{fake_mcp.base_url}/as/register"
    assert server.resource == f"{fake_mcp.base_url}/mcp"
    assert server.scopes == ("docs.read",)


def test_discover_falls_back_to_legacy_endpoints():
    server = discover("https://mcp.example.com/v1/mcp", fetch_json=lambda _url: None)

    assert server.authorization_endpoint == "https://mcp.example.com/authorize"
    assert server.token_endpoint == "https://mcp.example.com/token"
    assert server.registration_endpoint == "https://mcp.example.com/register"
    assert server.resource == "https://mcp.example.com/v1/mcp"


def test_oauth_endpoints_require_https_except_loopback():
    assert checked_endpoint("http://127.0.0.1:9/token", "Token-URL")
    assert checked_endpoint("https://login.example.com/token", "Token-URL")
    with pytest.raises(McpOAuthError):
        checked_endpoint("http://login.example.com/token", "Token-URL")
    with pytest.raises(McpOAuthError):
        discover(
            "https://mcp.example.com/mcp",
            fetch_json=lambda _url: None,
            authorization_endpoint="https://login.example.com/authorize",
            token_endpoint="http://login.example.com/token",
        )


# ---------------------------------------------------------------------------
# Werkzeugnamen
# ---------------------------------------------------------------------------


def test_exposed_tool_names_are_prefixed_unique_and_short():
    names = exposed_tool_names("dms", ["search", "files.read", "files_read", "x" * 80])

    assert names["search"] == "dms__search"
    assert names["files.read"] == "dms__files_read"
    assert names["files_read"] != names["files.read"]
    assert all(len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names.values())


def test_internal_tools_never_look_external():
    assert not any(is_external_tool_name(tool.name) for tool in TOOLS)


# ---------------------------------------------------------------------------
# Verwaltung über API und UI
# ---------------------------------------------------------------------------


def test_admin_manages_server_and_global_token_via_api(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="X-Api-Key", required_auth="geheim-token-123")
    server_id = _token_server(app, fake_mcp, header="X-Api-Key")
    client = app.test_client()

    response = client.post(
        f"/api/v1/mcp-servers/{server_id}/credential",
        json={"scope": "global", "token": "geheim-token-123"},
        headers=_auth("admin"),
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["global_credential"]["status"] == "connected"
    assert body["global_credential"]["token_last4"] == "-123"
    assert "geheim-token-123" not in response.get_data(as_text=True)
    # Mit dem neuen Zugang werden die Werkzeuge gleich abgerufen (prüft das Token).
    assert len(body["tools"]) == 2 and "tools_refresh_error" not in body

    response = client.post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))
    assert response.status_code == 200
    tools = response.get_json()["tools"]
    assert [tool["name"] for tool in tools] == ["search_documents", "move_document"]
    assert [tool["requires_confirmation"] for tool in tools] == [False, True]
    assert response.get_json()["server_info"]["name"] == "fake-dms"

    # Token liegt nur verschlüsselt vor.
    with app.app_context():
        credential = _credential(app, server_id, None)
        assert "geheim-token-123" not in credential.secret_encrypted
        assert _decrypt(credential.secret_encrypted) == {"token": "geheim-token-123"}

    # Buchhalter sehen aktive Server ohne Einstellungen, ändern aber nichts.
    response = client.get("/api/v1/mcp-servers", headers=_auth("anna"))
    listed = response.get_json()["mcp_servers"]
    assert [server["name"] for server in listed] == ["DMS"]
    assert "oauth" not in listed[0] and listed[0]["can_manage"] is False
    assert listed[0]["global_credential"] == {"status": "connected", "status_label": "Verbunden"}
    assert client.post(
        "/api/v1/mcp-servers", json={"name": "X", "url": "https://x.example/mcp"},
        headers=_auth("anna"),
    ).status_code == 403
    assert client.post(
        f"/api/v1/mcp-servers/{server_id}/credential",
        json={"scope": "global", "token": "fremd"},
        headers=_auth("anna"),
    ).status_code == 403


def _public_resolver(host, _port):
    """DNS-Ersatz: IP-Literale wie angegeben, Namen als öffentliche Adresse."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        return [ipaddress.ip_address("93.184.215.14")]


def test_tenant_admin_cannot_change_instance_wide_server(tmp_path, fake_mcp, monkeypatch):
    monkeypatch.setattr(mcp_client, "resolve_host", _public_resolver)
    app = _create_app(tmp_path)
    server_id = _token_server(app, fake_mcp)
    client = app.test_client()

    response = client.patch(
        f"/api/v1/mcp-servers/{server_id}", json={"name": "Neu"}, headers=_auth("chef")
    )
    assert response.status_code == 403

    response = client.post(
        "/api/v1/mcp-servers",
        json={"name": "CRM", "url": "https://crm.example.com/mcp/s3cr3t", "tenant_id": None},
        headers=_auth("chef"),
    )
    assert response.status_code == 201
    assert response.get_json()["tenant_id"] == app.config["_TEST_TENANT_ID"]
    assert response.get_json()["tool_prefix"] == "crm"

    # Interne Adressen trägt nur ein globaler Administrator ein (kein Abtasten des Netzes).
    response = client.post(
        "/api/v1/mcp-servers",
        json={"name": "Intern", "url": f"{fake_mcp.base_url}/mcp"},
        headers=_auth("chef"),
    )
    assert response.status_code == 400
    assert "interne Adresse" in response.get_json()["error"]

    # Im Audit-Log steht von der URL nur der Ursprung (Schlüssel stecken oft im Pfad).
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.entity_type == "mcp_server")
        ).scalar_one()
        assert entry.payload["url_origin"] == "https://crm.example.com"
        assert "s3cr3t" not in json.dumps(entry.payload)


def test_changing_connection_settings_drops_credentials(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    server_id = _token_server(app, fake_mcp)
    client = app.test_client()
    client.post(
        f"/api/v1/mcp-servers/{server_id}/credential", json={"token": "abc12345"},
        headers=_auth("admin"),
    )

    response = client.patch(
        f"/api/v1/mcp-servers/{server_id}", json={"description": "Neu"}, headers=_auth("admin")
    )
    assert response.get_json()["credentials_reset"] is False
    assert _credential(app, server_id, None) is not None

    response = client.patch(
        f"/api/v1/mcp-servers/{server_id}",
        json={"url": f"{fake_mcp.base_url}/anders"},
        headers=_auth("admin"),
    )
    assert response.get_json()["credentials_reset"] is True
    assert _credential(app, server_id, None) is None


def test_invalid_settings_are_rejected(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    client = app.test_client()
    cases = [
        {"name": "A", "url": "ftp://x.example/mcp"},
        {"name": "A", "url": "https://user:pw@x.example/mcp"},
        {"name": "A", "url": "https://x.example/mcp", "tool_prefix": "A_B"},
        {"name": "A", "url": "https://x.example/mcp", "auth_type": "basic"},
        {"name": "A", "url": "https://x.example/mcp", "timeout_seconds": 1},
        {"name": "A", "url": "https://x.example/mcp", "oauth_token_url": "http://x.example/t"},
        {"name": "A", "url": "https://x.example/mcp", "token_header": "Host"},
        {"name": "A", "url": "https://x.example/mcp", "token_header": "X Api"},
    ]
    for payload in cases:
        response = client.post("/api/v1/mcp-servers", json=payload, headers=_auth("admin"))
        assert response.status_code == 400, payload

    _create_server(app, fake_mcp)
    response = client.post(
        "/api/v1/mcp-servers",
        json={"name": "B", "url": "https://x.example/mcp", "tool_prefix": "dms"},
        headers=_auth("admin"),
    )
    assert response.status_code == 400
    assert "bereits vergeben" in response.get_json()["error"]


def test_admin_page_creates_server_and_restricts_tools(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    client = _login(app, "admin")

    response = client.post(
        "/verwaltung/mcp-server",
        data={
            "name": "Ablage",
            "url": f"{fake_mcp.base_url}/mcp",
            "auth_type": "none",
            "confirm_mode": "write",
            "timeout_seconds": "30",
        },
    )
    assert response.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        server = session.execute(select(McpServer)).scalar_one()
        server_id = server.id
        assert server.tool_prefix == "ablage" and server.tenant_id is None

    client.post(f"/verwaltung/mcp-server/{server_id}/werkzeuge")
    page = client.get("/verwaltung/mcp-server").get_data(as_text=True)
    assert "search_documents" in page and "2 Werkzeuge" in page

    client.post(f"/verwaltung/mcp-server/{server_id}/freigabe", data={"tools": "search_documents"})
    with app.extensions["db_session_factory"]() as session:
        assert session.get(McpServer, server_id).allowed_tools == ["search_documents"]

    # Buchhalter sehen die Verwaltungsseite nur mit Hinweis.
    page = _login(app, "anna").get("/verwaltung/mcp-server").get_data(as_text=True)
    assert "nur ein Administrator" in page


def test_demo_mode_blocks_mcp_server_changes(tmp_path):
    app = _create_app(tmp_path, DEMO_MODE=True)
    response = app.test_client().post(
        "/api/v1/mcp-servers",
        json={"name": "X", "url": "https://x.example/mcp"},
        headers=_auth("admin"),
    )
    assert response.status_code == 403
    assert response.get_json()["demo"] is True


# ---------------------------------------------------------------------------
# Zugänge je Benutzer und Chat
# ---------------------------------------------------------------------------


def _chat(client, app, bodies: list[dict], message: str = "Bitte prüfen"):
    poster = _FakePoster(bodies)
    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={"company_id": str(app.config["_TEST_COMPANY_ID"]), "message": message},
        )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json(), poster


def test_chat_runs_external_read_tool_immediately(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer zentral-1")
    server_id = _token_server(app, fake_mcp)
    api = app.test_client()
    api.post(
        f"/api/v1/mcp-servers/{server_id}/credential", json={"token": "zentral-1"},
        headers=_auth("admin"),
    )
    api.post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))

    payload, poster = _chat(
        _login(app, "anna"),
        app,
        [
            _function_call_body("dms__search_documents", {"query": "Rechnung 42"}),
            _message_body("Die Rechnung liegt in der Ablage."),
        ],
    )

    call = payload["assistant_message"]["tool_calls"][0]
    assert call["status"] == "executed"
    assert call["title"] == "DMS: Dokumente suchen"
    assert fake_mcp.state["calls"] == [
        {
            "name": "search_documents",
            "arguments": {"query": "Rechnung 42"},
            "auth": "Bearer zentral-1",
        }
    ]
    first = poster.payloads[0]
    definitions = {tool["name"]: tool for tool in first["tools"]}
    assert "$schema" not in definitions["dms__search_documents"]["parameters"]
    assert definitions["dms__search_documents"]["description"].startswith(
        "[Externes System DMS]"
    )
    assert "Bestätigung" in definitions["dms__move_document"]["description"]
    system_prompt = first["input"][0]["content"][0]["text"]
    assert "=== Externe Systeme (MCP-Server) ===" in system_prompt
    assert "- DMS (Präfix dms__): Belegablage; Eingangsrechnungen im Ordner Eingang." in (
        system_prompt
    )
    outputs = [item for item in poster.payloads[1]["input"]
               if item.get("type") == "function_call_output"]
    assert UNTRUSTED_NOTICE in outputs[0]["output"]
    assert "Ergebnis search_documents" in outputs[0]["output"]


def test_chat_confirms_external_write_tool_and_audits_it(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    server_id = _create_server(app, fake_mcp)  # ohne Anmeldung
    app.test_client().post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))
    client = _login(app, "anna")

    payload, _poster = _chat(
        client,
        app,
        [_function_call_body("dms__move_document", {"document_id": "42", "folder": "Archiv"})],
    )
    message = payload["assistant_message"]
    assert message["pending_action"]["title"] == "DMS: move_document"
    assert fake_mcp.state["calls"] == []

    poster = _FakePoster([_message_body("Verschoben.")])
    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(f"/chat/actions/{message['id']}/confirm")
    assert response.status_code == 200
    assert response.get_json()["updated_message"]["tool_calls"][0]["status"] == "confirmed"
    assert fake_mcp.state["calls"][0]["name"] == "move_document"

    with app.extensions["db_session_factory"]() as session:
        entries = session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "mcp_server", AuditLog.action == "tool_called"
            )
        ).scalars().all()
        assert len(entries) == 1
        assert entries[0].payload["tool"] == "move_document"
        assert entries[0].changed_by == "anna"


def test_personal_tokens_are_per_user(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(
        auth_header="Authorization", required_auth={"Bearer anna-token", "Bearer admin-token"}
    )
    server_id = _token_server(app, fake_mcp, scope="user")
    api = app.test_client()

    # Der Administrator verwaltet hier keinen zentralen Zugang.
    response = api.post(
        f"/api/v1/mcp-servers/{server_id}/credential",
        json={"scope": "global", "token": "zentral"},
        headers=_auth("admin"),
    )
    assert response.status_code == 400

    anna = _login(app, "anna")
    response = anna.post(f"/obi-verbindungen/{server_id}/token", data={"token": "anna-token"})
    assert response.status_code == 302
    assert _credential(app, server_id, _user_id(app, "anna")) is not None
    page = anna.get("/obi-verbindungen").get_data(as_text=True)
    assert "Verbunden" in page and "…oken" in page and "der Zugang funktioniert" in page
    # Annas Zugang wurde geprüft, legt aber nicht die Werkzeuge für alle fest.
    with app.extensions["db_session_factory"]() as session:
        assert session.get(McpServer, server_id).tools_cache is None
    _payload, poster = _chat(anna, app, [_message_body("Noch nichts da.")])
    assert "DMS (Werkzeuge noch nicht abgerufen)" in (
        poster.payloads[0]["input"][0]["content"][0]["text"]
    )

    # Der Administrator ruft die Werkzeuge mit seinem eigenen Zugang ab.
    response = api.post(
        f"/api/v1/mcp-servers/{server_id}/credential",
        json={"scope": "user", "token": "admin-token"},
        headers=_auth("admin"),
    )
    assert response.status_code == 200 and len(response.get_json()["tools"]) == 2

    # Bernd ist nicht angemeldet: kein Werkzeug, aber ein Hinweis im Systemprompt.
    _payload, poster = _chat(_login(app, "bernd"), app, [_message_body("Bitte anmelden.")])
    assert not any(tool["name"].startswith("dms__") for tool in poster.payloads[0]["tools"])
    assert "Für DMS ist der Benutzer noch nicht angemeldet" in (
        poster.payloads[0]["input"][0]["content"][0]["text"]
    )

    # Anna: Werkzeugliste wird mit ihrem Zugang geholt, Aufruf mit ihrem Token.
    _chat(
        anna,
        app,
        [_function_call_body("dms__search_documents", {"query": "x"}), _message_body("Ok.")],
    )
    assert fake_mcp.state["calls"][0]["auth"] == "Bearer anna-token"

    # Prüfer können keine eigenen Zugänge anlegen (kein Chat).
    response = api.post(
        f"/api/v1/mcp-servers/{server_id}/credential",
        json={"token": "p"},
        headers=_auth("paula"),
    )
    assert response.status_code == 403

    response = anna.post(f"/obi-verbindungen/{server_id}/abmelden")
    assert response.status_code == 302
    assert _credential(app, server_id, _user_id(app, "anna")) is None


def test_rejected_token_is_reported(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer richtig")
    server_id = _token_server(app, fake_mcp)
    api = app.test_client()
    response = api.post(
        f"/api/v1/mcp-servers/{server_id}/credential", json={"token": "falsch"},
        headers=_auth("admin"),
    )
    assert "abgelehnt" in response.get_json()["tools_refresh_error"]

    response = api.post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))

    assert response.status_code == 502
    assert "abgelehnt" in response.get_json()["error"]
    body = api.get("/api/v1/mcp-servers", headers=_auth("admin")).get_json()["mcp_servers"][0]
    assert body["global_credential"]["status"] == "error"


# ---------------------------------------------------------------------------
# OAuth-Anmeldung je Benutzer
# ---------------------------------------------------------------------------


def _authorization_params(page: str) -> dict[str, str]:
    match = re.search(r'href="([^"]+)" data-auto-follow', page)
    assert match, page
    url = html.unescape(match.group(1))
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def test_oauth_login_per_user_with_refresh(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer niemand")
    server_id = _create_server(app, fake_mcp, auth_type="oauth", credential_scope="user")
    # Der Administrator meldet sich persönlich an – sein Abruf legt die Werkzeugliste fest.
    owner = _login(app, "admin")

    response = owner.post(f"/obi-verbindungen/{server_id}/oauth")
    assert response.status_code == 200
    params = _authorization_params(response.get_data(as_text=True))
    assert params["client_id"] == "client-1"
    assert params["code_challenge_method"] == "S256"
    assert params["resource"] == f"{fake_mcp.base_url}/mcp"
    assert params["scope"] == "docs.read"
    assert params["redirect_uri"] == "http://localhost/obi-verbindungen/oauth/callback"
    assert fake_mcp.state["registrations"][0]["redirect_uris"] == [params["redirect_uri"]]

    # Der Anbieter leitet mit einem Code zurück; nur wer begonnen hat, kann ihn einlösen.
    fake_mcp.state["codes"]["code-1"] = {
        "challenge": params["code_challenge"],
        "redirect_uri": params["redirect_uri"],
    }
    callback = f"/obi-verbindungen/oauth/callback?code=code-1&state={params['state']}"
    response = _login(app, "bernd").get(callback)
    assert response.status_code == 302
    assert fake_mcp.state["token_requests"] == []

    response = owner.get(callback)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/obi-verbindungen")
    token_request = fake_mcp.state["token_requests"][0]
    assert token_request["resource"] == [f"{fake_mcp.base_url}/mcp"]
    credential = _credential(app, server_id, _user_id(app, "admin"))
    assert credential.kind == "oauth" and credential.auth_state is None
    # Gleich nach der Anmeldung sind die Werkzeuge abgerufen.
    with app.extensions["db_session_factory"]() as session:
        assert len(session.get(McpServer, server_id).tools_cache) == 2
    assert fake_mcp.state["requests"][-1]["auth"] == "Bearer at-1"
    with app.app_context():
        assert _decrypt(credential.secret_encrypted)["access_token"] == "at-1"

    # Chat: Werkzeuge mit dem eigenen Token, beim abgelaufenen Token erst erneuern.
    _chat(
        owner,
        app,
        [_function_call_body("dms__search_documents", {"query": "x"}), _message_body("Ok.")],
    )
    assert fake_mcp.state["calls"][-1]["auth"] == "Bearer at-1"

    with app.app_context(), app.extensions["db_session_factory"]() as session:
        stored = session.get(McpCredential, credential.id)
        secret = _decrypt(stored.secret_encrypted)
        secret["expires_at"] = int(time.time()) - 10
        stored.secret_encrypted = _encrypt(secret)
        session.commit()

    _chat(
        owner,
        app,
        [_function_call_body("dms__search_documents", {"query": "y"}), _message_body("Ok.")],
    )
    assert fake_mcp.state["calls"][-1]["auth"] == "Bearer at-2"
    assert fake_mcp.state["token_requests"][-1]["grant_type"] == ["refresh_token"]


def test_oauth_relogin_needed_when_refresh_is_rejected(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer niemand")
    server_id = _create_server(app, fake_mcp, auth_type="oauth", credential_scope="global")
    admin = _login(app, "admin")
    params = _authorization_params(
        admin.post(f"/verwaltung/mcp-server/{server_id}/oauth").get_data(as_text=True)
    )
    fake_mcp.state["codes"]["code-1"] = {
        "challenge": params["code_challenge"],
        "redirect_uri": params["redirect_uri"],
    }
    response = admin.get(f"/obi-verbindungen/oauth/callback?code=code-1&state={params['state']}")
    assert response.headers["Location"].endswith(f"/verwaltung/mcp-server#mcp-{server_id}")
    assert _credential(app, server_id, None).secret_encrypted is not None

    # Der Anbieter widerruft: Token ungültig, Refresh-Token ebenfalls.
    fake_mcp.state.update(required_auth="Bearer widerrufen", valid_refresh="rt-anders")
    response = app.test_client().post(
        f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin")
    )

    assert response.status_code == 502
    assert "erneut anmelden" in response.get_json()["error"]
    credential = _credential(app, server_id, None)
    assert credential.secret_encrypted is None
    body = app.test_client().get("/api/v1/mcp-servers", headers=_auth("anna")).get_json()
    assert body["mcp_servers"][0]["global_credential"]["status"] == "expired"


# ---------------------------------------------------------------------------
# Härtung nach Review: Verbindungen, Metadaten, Fehlerfälle
# ---------------------------------------------------------------------------


def _admin_oauth_login(app, fake, server_id: int, *, iss: str | None = None):
    """Zentrale OAuth-Anmeldung des Administrators bis zum Rücksprung."""
    admin = _login(app, "admin")
    params = _authorization_params(
        admin.post(f"/verwaltung/mcp-server/{server_id}/oauth").get_data(as_text=True)
    )
    fake.state["codes"]["code-1"] = {
        "challenge": params["code_challenge"],
        "redirect_uri": params["redirect_uri"],
    }
    callback = f"/obi-verbindungen/oauth/callback?code=code-1&state={params['state']}"
    if iss is not None:
        callback += f"&iss={iss}"
    return admin, admin.get(callback)


def test_session_does_not_follow_redirects(fake_mcp):
    session = McpSession(f"{fake_mcp.base_url}/redirect", headers={"Authorization": "Bearer x"})

    with pytest.raises(McpClientError, match="leitet"):
        session.list_tools()

    assert fake_mcp.state["requests"] == []


def test_session_blocks_link_local_addresses():
    session = McpSession("http://169.254.169.254/mcp", timeout=2)

    with pytest.raises(McpClientError, match="nicht zulässig"):
        session.list_tools()


def test_expired_session_on_notification_is_a_client_error(fake_mcp):
    fake_mcp.state["expire_on_notification"] = True
    session = McpSession(f"{fake_mcp.base_url}/mcp", timeout=5)

    with pytest.raises(McpClientError):
        session.list_tools()


def test_call_deadline_covers_endless_streams(fake_mcp):
    fake_mcp.state["sse_slow"] = True
    session = McpSession(f"{fake_mcp.base_url}/mcp", timeout=0.5)
    session.initialize()

    started = time.monotonic()
    with pytest.raises(McpClientError, match="nicht rechtzeitig"):
        session.call_tool("search_documents", {"query": "x"})

    assert time.monotonic() - started < 2.5


def test_resource_metadata_must_belong_to_the_server(fake_mcp):
    assert resource_matches("https://mcp.example.com", "https://mcp.example.com/v1/mcp")
    assert resource_matches("https://mcp.example.com/v1/mcp/", "https://mcp.example.com/v1/mcp")
    assert not resource_matches("https://mcp.example.com/v2", "https://mcp.example.com/v1/mcp")
    assert not resource_matches("https://graph.example.com/", "https://mcp.example.com/mcp")

    fake_mcp.state["prm_resource"] = "https://graph.example.com/"
    with pytest.raises(McpOAuthError, match="gehören nicht"):
        discover(f"{fake_mcp.base_url}/mcp")


def test_authorization_server_metadata_must_name_the_issuer(fake_mcp):
    fake_mcp.state["as_issuer"] = "https://login.example.com"
    with pytest.raises(McpOAuthError, match="anderen Aussteller"):
        discover(f"{fake_mcp.base_url}/mcp")


def test_cross_origin_metadata_hint_is_ignored():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return None

    discover(
        "https://mcp.example.com/mcp",
        www_authenticate='Bearer resource_metadata="https://elsewhere.example.org/meta"',
        fetch_json=fetch,
    )
    assert "https://elsewhere.example.org/meta" not in fetched
    assert fetched[0] == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"


def test_authenticated_servers_require_https(tmp_path):
    app = _create_app(tmp_path)
    response = app.test_client().post(
        "/api/v1/mcp-servers",
        json={"name": "A", "url": "http://dms.example.com/mcp", "auth_type": "token"},
        headers=_auth("admin"),
    )
    assert response.status_code == 400
    assert "HTTPS" in response.get_json()["error"]


def test_server_url_is_only_shown_to_managers(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    _create_server(app, fake_mcp)
    client = app.test_client()

    listed = client.get("/api/v1/mcp-servers", headers=_auth("anna")).get_json()["mcp_servers"]
    assert "url" not in listed[0]
    listed = client.get("/api/v1/mcp-servers", headers=_auth("admin")).get_json()["mcp_servers"]
    assert listed[0]["url"] == f"{fake_mcp.base_url}/mcp"


def test_missing_scope_keeps_the_oauth_login(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer niemand")
    server_id = _create_server(app, fake_mcp, auth_type="oauth", credential_scope="global")
    _admin_oauth_login(app, fake_mcp, server_id)
    fake_mcp.state["forbid_tool"] = "search_documents"

    payload, _poster = _chat(
        _login(app, "anna"),
        app,
        [_function_call_body("dms__search_documents", {"query": "x"}), _message_body("Ok.")],
    )

    call = payload["assistant_message"]["tool_calls"][0]
    assert call["is_error"] is True and "verweigert" in call["result_text"]
    credential = _credential(app, server_id, None)
    assert credential.secret_encrypted is not None and credential.last_error is None


def test_callback_checks_the_issuer(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(
        auth_header="Authorization", required_auth="Bearer niemand", iss_supported=True
    )
    server_id = _create_server(app, fake_mcp, auth_type="oauth", credential_scope="global")

    _admin_oauth_login(app, fake_mcp, server_id)  # ohne iss
    assert _credential(app, server_id, None).secret_encrypted is None
    _admin_oauth_login(app, fake_mcp, server_id, iss="https://login.example.com")
    assert _credential(app, server_id, None).secret_encrypted is None
    assert fake_mcp.state["token_requests"] == []

    _admin_oauth_login(app, fake_mcp, server_id, iss=f"{fake_mcp.base_url}/as")
    assert _credential(app, server_id, None).secret_encrypted is not None


def test_unknown_client_is_registered_again(tmp_path, fake_mcp):
    app = _create_app(tmp_path)
    fake_mcp.state.update(
        auth_header="Authorization", required_auth="Bearer niemand", token_error="invalid_client"
    )
    server_id = _create_server(app, fake_mcp, auth_type="oauth", credential_scope="global")

    _admin_oauth_login(app, fake_mcp, server_id)
    with app.app_context(), app.extensions["db_session_factory"]() as session:
        assert _decrypt(session.get(McpServer, server_id).oauth_registration_encrypted) is None

    fake_mcp.state["token_error"] = None
    _admin_oauth_login(app, fake_mcp, server_id)
    assert len(fake_mcp.state["registrations"]) == 2
    assert _credential(app, server_id, None).secret_encrypted is not None


def test_broken_server_does_not_hide_the_others(tmp_path, fake_mcp, monkeypatch):
    app = _create_app(tmp_path)
    first = _create_server(app, fake_mcp, name="Alpha", tool_prefix="alpha")
    second = _create_server(app, fake_mcp, name="Beta", tool_prefix="beta")
    for server_id in (first, second):
        app.test_client().post(
            f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin")
        )
    original = mcp_connections._chat_tools

    def broken(server):
        if server.name == "Alpha":
            raise RuntimeError("kaputt")
        return original(server)

    monkeypatch.setattr(mcp_connections, "_chat_tools", broken)
    _payload, poster = _chat(_login(app, "anna"), app, [_message_body("Ok.")])

    names = {tool["name"] for tool in poster.payloads[0]["tools"]}
    assert "beta__search_documents" in names and "alpha__search_documents" not in names
    system_prompt = poster.payloads[0]["input"][0]["content"][0]["text"]
    assert "Alpha (derzeit nicht erreichbar)" in system_prompt
