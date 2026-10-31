"""OpenBuchhaltung als MCP-Proxy und Firmenwissen über MCP.

Der MCP-Server von OpenBuchhaltung reicht die Werkzeuge externer MCP-Server mit
``proxy_mode`` an seine Clients weiter (mit dem Zugang des aufrufenden Benutzers). Das
Firmenwissen einer Gesellschaft ist über MCP nur lesbar, solange sie es freigibt.
"""

from __future__ import annotations

import json
from email.message import Message
from unittest.mock import patch

from sqlalchemy import select
from test_chat import _FakePoster, _function_call_body, _message_body
from test_mcp_connections import (  # noqa: F401 – fake_mcp ist eine Fixture
    _auth,
    _create_app,
    _create_server,
    _login,
    fake_mcp,
)

from app.services import mcp_server
from app.services.mcp_server import MCP_CHANNEL_HEADER, HttpApiClient
from domain.models import AuditLog, Company


def _bridge(app, username: str, method: str, params: dict | None = None) -> dict:
    """JSON-RPC über die MCP-Bridge der App (gleiche Regeln wie der MCP-Server)."""
    response = app.test_client().post(
        "/api/v1/mcp/call",
        headers=_auth(username),
        json={"method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


def _tool_names(app, username: str) -> set[str]:
    return {tool["name"] for tool in _bridge(app, username, "tools/list")["tools"]}


def _proxy_server(
    app, fake, *, proxy_mode: str, auth_type: str = "token", scope: str = "global"
) -> int:
    return _create_server(
        app, fake, auth_type=auth_type, credential_scope=scope, proxy_mode=proxy_mode
    )


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


def test_proxy_lists_and_passes_calls_through(tmp_path, fake_mcp):  # noqa: F811
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer zentral-1")
    server_id = _proxy_server(app, fake_mcp, proxy_mode="all")
    app.test_client().post(
        f"/api/v1/mcp-servers/{server_id}/credential", json={"token": "zentral-1"},
        headers=_auth("admin"),
    )

    tools = {tool["name"]: tool for tool in _bridge(app, "anna", "tools/list")["tools"]}
    assert {"dms__search_documents", "dms__move_document", "list_accounts"} <= set(tools)
    assert tools["dms__search_documents"]["annotations"] == {"readOnlyHint": True}
    assert tools["dms__search_documents"]["title"] == "DMS: Dokumente suchen"
    assert tools["dms__move_document"]["description"].startswith("[Externes System DMS]")

    result = _bridge(
        app,
        "anna",
        "tools/call",
        {"name": "dms__move_document", "arguments": {"document_id": "42", "folder": "Archiv"}},
    )
    # Inhaltsblöcke unverändert durchgereicht, auch nicht-textliche.
    assert result["isError"] is False
    assert result["content"][0] == {"type": "text", "text": "Ergebnis move_document"}
    assert result["content"][1]["type"] == "resource_link"
    assert fake_mcp.state["calls"][-1] == {
        "name": "move_document",
        "arguments": {"document_id": "42", "folder": "Archiv"},
        "auth": "Bearer zentral-1",
    }
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == "tool_called")
        ).scalar_one()
        assert entry.payload["channel"] == "mcp" and entry.changed_by == "anna"


def test_proxy_read_mode_passes_only_read_tools(tmp_path, fake_mcp):  # noqa: F811
    app = _create_app(tmp_path)
    server_id = _proxy_server(app, fake_mcp, proxy_mode="read", auth_type="none")
    app.test_client().post(
        f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin")
    )

    names = _tool_names(app, "anna")
    assert "dms__search_documents" in names and "dms__move_document" not in names

    result = _bridge(
        app, "anna", "tools/call", {"name": "dms__move_document", "arguments": {}}
    )
    assert result["isError"] is True
    assert "nicht weitergereicht" in result["content"][0]["text"]
    assert fake_mcp.state["calls"] == []


def test_proxy_is_off_by_default(tmp_path, fake_mcp):  # noqa: F811
    app = _create_app(tmp_path)
    server_id = _create_server(app, fake_mcp)
    app.test_client().post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))

    assert not any("__" in name for name in _tool_names(app, "anna"))
    result = _bridge(
        app, "anna", "tools/call", {"name": "dms__search_documents", "arguments": {"query": "x"}}
    )
    assert result["isError"] is True and fake_mcp.state["calls"] == []


def test_proxy_uses_the_personal_login_of_the_caller(tmp_path, fake_mcp):  # noqa: F811
    app = _create_app(tmp_path)
    fake_mcp.state.update(
        auth_header="Authorization", required_auth={"Bearer anna-token", "Bearer admin-token"}
    )
    server_id = _proxy_server(app, fake_mcp, proxy_mode="all", scope="user")
    client = app.test_client()
    for username in ("admin", "anna"):
        client.post(
            f"/api/v1/mcp-servers/{server_id}/credential",
            json={"scope": "user", "token": f"{username}-token"},
            headers=_auth(username),
        )

    _bridge(app, "anna", "tools/call", {"name": "dms__search_documents", "arguments": {}})
    assert fake_mcp.state["calls"][-1]["auth"] == "Bearer anna-token"
    # Bernd hat keinen eigenen Zugang: Die Werkzeuge fehlen für ihn.
    assert not any(name.startswith("dms__") for name in _tool_names(app, "bernd"))


def test_proxy_needs_a_write_role(tmp_path, fake_mcp):  # noqa: F811
    app = _create_app(tmp_path)
    _proxy_server(app, fake_mcp, proxy_mode="all")
    client = app.test_client()

    response = client.get("/api/v1/mcp-proxy/tools", headers=_auth("paula"))
    assert response.status_code == 200 and response.get_json()["tools"] == []
    response = client.post(
        "/api/v1/mcp-proxy/tools/call",
        json={"name": "dms__search_documents", "arguments": {}},
        headers=_auth("paula"),
    )
    assert response.status_code == 403


def test_chat_executes_external_tools_itself(tmp_path, fake_mcp):  # noqa: F811
    """Obi braucht den Proxy nicht: Der Chat führt externe Werkzeuge selbst aus (mit
    Bestätigungspflicht), auch wenn sie nicht weitergereicht werden."""
    app = _create_app(tmp_path)
    server_id = _create_server(app, fake_mcp)
    app.test_client().post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))
    poster = _FakePoster(
        [_function_call_body("dms__search_documents", {"query": "x"}), _message_body("Ok.")]
    )
    with patch("app.services.chat_llm._post_json", poster):
        response = _login(app, "anna").post(
            "/chat/send",
            data={"company_id": str(app.config["_TEST_COMPANY_ID"]), "message": "Suche"},
        )
    assert response.get_json()["assistant_message"]["tool_calls"][0]["status"] == "executed"


def test_http_transport_marks_its_requests_as_mcp(monkeypatch):
    seen = {}

    headers = Message()
    headers["Content-Type"] = "application/json"

    class _Response:
        status = 200

        headers = None

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        seen.update(request.headers)
        response = _Response()
        response.headers = headers
        return response

    monkeypatch.setattr(mcp_server, "urlopen", fake_urlopen)
    HttpApiClient("http://api.test/api/v1").call("GET", "/companies")
    assert seen[MCP_CHANNEL_HEADER.capitalize()] == "mcp"


# ---------------------------------------------------------------------------
# Firmenwissen über MCP
# ---------------------------------------------------------------------------


def _knowledge_entry(app) -> int:
    company_id = app.config["_TEST_COMPANY_ID"]
    response = app.test_client().post(
        f"/api/v1/companies/{company_id}/knowledge/entries",
        json={"title": "Reisekosten", "content": "Bahn 2. Klasse, Flüge nur mit Freigabe."},
        headers=_auth("anna"),
    )
    assert response.status_code == 201
    return response.get_json()["id"]


def test_company_knowledge_can_be_closed_for_mcp(tmp_path):
    app = _create_app(tmp_path)
    company_id = app.config["_TEST_COMPANY_ID"]
    entry_id = _knowledge_entry(app)
    client = app.test_client()

    # Freigegeben (Standard): über MCP les- und durchsuchbar.
    listed = _bridge(
        app, "anna", "tools/call",
        {"name": "list_company_knowledge_entries", "arguments": {"company_id": company_id}},
    )
    assert listed["isError"] is False and "Reisekosten" in listed["content"][0]["text"]
    companies = json.loads(
        _bridge(app, "anna", "tools/call", {"name": "list_companies", "arguments": {}})[
            "content"
        ][0]["text"]
    )
    assert companies[0]["knowledge_mcp_enabled"] is True

    # Nur Administratoren sperren, und nie über MCP selbst.
    url = f"/api/v1/companies/{company_id}/knowledge/mcp"
    assert client.post(url, json={"enabled": False}, headers=_auth("anna")).status_code == 403
    mcp_headers = {**_auth("admin"), MCP_CHANNEL_HEADER: "mcp"}
    assert client.post(url, json={"enabled": False}, headers=mcp_headers).status_code == 403
    response = client.post(url, json={"enabled": False}, headers=_auth("admin"))
    assert response.status_code == 200 and response.get_json()["enabled"] is False

    # Über MCP gesperrt …
    for name, arguments in (
        ("list_company_knowledge_entries", {"company_id": company_id}),
        ("get_company_knowledge_entry", {"entry_id": entry_id}),
        ("list_company_knowledge_sources", {"company_id": company_id}),
    ):
        result = _bridge(app, "anna", "tools/call", {"name": name, "arguments": arguments})
        assert result["isError"] is True, name
        assert "nicht freigegeben" in result["content"][0]["text"]
    search = json.loads(
        _bridge(
            app, "anna", "tools/call",
            {"name": "search_knowledge", "arguments": {"query": "Reisekosten",
                                                       "company_id": company_id}},
        )["content"][0]["text"]
    )
    assert search["company_results"] == [] and "nicht freigegeben" in search["company"]["note"]
    # … auch für den HTTP-Transport des MCP-Servers (Kanal-Header).
    entry_url = f"/api/v1/knowledge-entries/{entry_id}"
    response = client.get(entry_url, headers={**_auth("anna"), MCP_CHANNEL_HEADER: "mcp"})
    assert response.status_code == 403

    # REST ohne MCP-Kanal und Obi im KI-Chat bleiben unberührt.
    assert client.get(entry_url, headers=_auth("anna")).status_code == 200
    poster = _FakePoster(
        [
            _function_call_body("get_company_knowledge_entry", {"entry_id": entry_id}),
            _message_body("Bahn 2. Klasse."),
        ]
    )
    with patch("app.services.chat_llm._post_json", poster):
        response = _login(app, "anna").post(
            "/chat/send", data={"company_id": str(company_id), "message": "Reisekosten?"}
        )
    call = response.get_json()["assistant_message"]["tool_calls"][0]
    assert call["is_error"] is False and "Reisekosten" in call["result_text"]

    with app.extensions["db_session_factory"]() as session:
        assert session.get(Company, company_id).knowledge_mcp_enabled is False
        assert session.execute(
            select(AuditLog).where(AuditLog.action == "knowledge_mcp_disabled")
        ).scalar_one().changed_by == "admin"


def test_company_knowledge_mcp_switch_in_the_ui(tmp_path):
    app = _create_app(tmp_path)
    company_id = app.config["_TEST_COMPANY_ID"]

    page = _login(app, "anna").get(f"/firmenwissen?company_id={company_id}").get_data(
        as_text=True
    )
    assert "freigegeben" in page and "Für MCP sperren" not in page

    admin = _login(app, "admin")
    response = admin.post("/firmenwissen/mcp", data={"company_id": company_id, "enabled": "0"})
    assert response.status_code == 302
    page = admin.get(f"/firmenwissen?company_id={company_id}").get_data(as_text=True)
    assert "gesperrt" in page and "Für MCP freigeben" in page
    with app.extensions["db_session_factory"]() as session:
        assert session.get(Company, company_id).knowledge_mcp_enabled is False
