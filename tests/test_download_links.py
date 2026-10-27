"""Download-Links: Belege und Exporte per signiertem Link statt nur als Base64."""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest
from document_files import pdf_document_bytes
from itsdangerous import TimestampSigner
from werkzeug.serving import make_server as make_wsgi_server

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services.chat_stream import stream_job
from app.services.download_links import DownloadGrant, issue_download_token
from app.services.internal_api import InProcessApiClient, internal_api_base_url
from app.services.mcp_http import make_server as make_mcp_server
from app.services.mcp_server import (
    ApiResponse,
    HttpApiClient,
    MCPServer,
    public_base_url_override,
)
from domain.models import Company, Tenant, User

GLOBAL_TOKEN = "global-backend-token"
MARIA_TOKEN = "obk_maria-token"
BERND_TOKEN = "obk_bernd-token"
MCP_SECRET = "mcp-secret"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _setup(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'download_links.db'}",
            "API_REQUIRE_AUTH": True,
            "API_AUTH_TOKEN": GLOBAL_TOKEN,
            "DOCUMENT_UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    with app.extensions["db_session_factory"]() as session:
        tenant_a = Tenant(name="Mandant A")
        tenant_b = Tenant(name="Mandant B")
        company = Company(name="A GmbH", currency_code="EUR", tenant=tenant_a)
        session.add_all([tenant_a, tenant_b, company])
        session.flush()
        for username, token, tenant in (
            ("maria", MARIA_TOKEN, tenant_a),
            ("bernd", BERND_TOKEN, tenant_b),
        ):
            session.add(
                User(
                    username=username,
                    password_hash=hash_password("geheim123"),
                    role="Buchhalter",
                    tenant_id=tenant.id,
                    api_token_hash=hash_api_token(token),
                    api_token_last4=token[-4:],
                )
            )
        session.commit()
        company_id = company.id

    file_bytes = pdf_document_bytes(b"download-link")
    upload = app.test_client().post(
        "/api/v1/documents",
        headers=_auth(MARIA_TOKEN),
        json={
            "company_id": company_id,
            "file_name": "Rechnung Müller.pdf",
            "mime_type": "application/pdf",
            "document_date": "2026-04-03",
            "content_base64": base64.b64encode(file_bytes).decode("ascii"),
        },
    )
    assert upload.status_code == 201, upload.get_json()
    return app, company_id, upload.get_json()["id"], file_bytes


def _document_link(client, document_id: int, token: str = MARIA_TOKEN):
    return client.get(f"/api/v1/documents/{document_id}/download-link", headers=_auth(token))


def test_document_link_downloads_without_login(tmp_path):
    app, _, document_id, file_bytes = _setup(tmp_path)
    client = app.test_client()

    response = _document_link(client, document_id)
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["file_name"] == "Rechnung_Muller.pdf"  # secure_filename beim Upload
    assert payload["integrity"]["matches"] is True
    assert payload["expires_in_seconds"] == 900
    assert "content_base64" not in payload
    assert payload["download_path"].startswith("/api/v1/downloads/")
    assert payload["download_url"] == f"http://localhost{payload['download_path']}"

    download = client.get(payload["download_path"])  # ohne Authorization-Header
    assert download.status_code == 200
    assert download.data == file_bytes
    assert download.mimetype == "application/pdf"
    assert download.headers["Content-Disposition"].startswith("attachment")
    assert "Rechnung" in download.headers["Content-Disposition"]
    assert download.headers["Cache-Control"] == "no-store"


def test_base64_content_keeps_working_and_carries_a_link(tmp_path):
    app, _, document_id, file_bytes = _setup(tmp_path)
    client = app.test_client()
    payload = client.get(
        f"/api/v1/documents/{document_id}/content", headers=_auth(MARIA_TOKEN)
    ).get_json()
    assert base64.b64decode(payload["content_base64"]) == file_bytes
    assert client.get(payload["download_path"]).data == file_bytes


def test_document_link_respects_tenant_scope(tmp_path):
    app, _, document_id, _ = _setup(tmp_path)
    assert _document_link(app.test_client(), document_id, BERND_TOKEN).status_code == 404


def test_link_stops_working_for_deactivated_user(tmp_path):
    app, _, document_id, _ = _setup(tmp_path)
    client = app.test_client()
    link = _document_link(client, document_id).get_json()["download_path"]

    with app.extensions["db_session_factory"]() as session:
        session.query(User).filter_by(username="maria").one().is_active = False
        session.commit()

    assert client.get(link).status_code == 403


def test_expired_link_is_rejected(tmp_path, monkeypatch):
    app, _, document_id, _ = _setup(tmp_path)
    client = app.test_client()
    with monkeypatch.context() as patch:
        patch.setattr(TimestampSigner, "get_timestamp", lambda self: int(time.time()) - 901)
        link = _document_link(client, document_id).get_json()["download_path"]

    response = client.get(link)
    assert response.status_code == 410
    assert "expired" in response.get_json()["error"]


def test_forged_or_foreign_tokens_are_rejected(tmp_path):
    app, _, document_id, _ = _setup(tmp_path)
    client = app.test_client()
    link = _document_link(client, document_id).get_json()["download_path"]

    tampered = link[:-1] + ("A" if link[-1] != "A" else "B")
    assert client.get(tampered).status_code == 404
    foreign = issue_download_token(
        "anderer-schluessel", DownloadGrant(path=f"/documents/{document_id}/download")
    )
    assert client.get(f"/api/v1/downloads/{foreign}").status_code == 404
    # Gültig signiert, aber kein Datei-Endpunkt: Links öffnen keine beliebigen API-Pfade.
    not_linkable = issue_download_token(
        app.config["SECRET_KEY"], DownloadGrant(path="/users", global_access=True)
    )
    assert client.get(f"/api/v1/downloads/{not_linkable}").status_code == 404


def test_export_download_link_matches_direct_export(tmp_path):
    app, company_id, _, _ = _setup(tmp_path)
    client = app.test_client()
    url = f"/api/v1/exports/journal.csv?company_id={company_id}"
    direct = client.get(url, headers=_auth(MARIA_TOKEN))
    assert direct.status_code == 200

    response = client.get(f"{url}&download_link=true", headers=_auth(MARIA_TOKEN))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["file_name"].endswith(".csv")
    assert payload["mime_type"] == "text/csv"
    assert payload["file_size_bytes"] == len(direct.data)

    download = client.get(payload["download_path"])
    assert download.status_code == 200
    assert download.data == direct.data


def test_export_link_reports_errors_immediately(tmp_path):
    app, _, _, _ = _setup(tmp_path)
    response = app.test_client().get(
        "/api/v1/exports/trial-balance.csv?download_link=1", headers=_auth(MARIA_TOKEN)
    )
    assert response.status_code == 400
    assert "download_url" not in response.get_json()


def test_mcp_bridge_links_use_the_callers_base_url(tmp_path):
    app, _, document_id, _ = _setup(tmp_path)
    response = app.test_client().post(
        "/api/v1/mcp/call",
        base_url="https://buch.example.de",
        headers=_auth(MARIA_TOKEN),
        json={
            "method": "tools/call",
            "params": {
                "name": "get_document_download_link",
                "arguments": {"document_id": document_id},
            },
        },
    )
    payload = json.loads(response.get_json()["result"]["content"][0]["text"])
    assert payload["download_url"].startswith("https://buch.example.de/api/v1/downloads/")


def test_chat_stream_thread_keeps_the_request_base_url(tmp_path):
    app, _, document_id, _ = _setup(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        user = session.query(User).filter_by(username="maria").one()
        maria = {"id": user.id, "username": "maria", "role": user.role, "tenant_id": user.tenant_id}

    def job(_report):
        client = InProcessApiClient(app, api_user=maria, global_access=False)
        return client.call("GET", f"/documents/{document_id}/download-link").json

    with app.test_request_context("/chat/send", base_url="https://chat.example.de"):
        response = stream_job(job)
    events = [json.loads(line) for line in response.response]
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["download_url"].startswith("https://chat.example.de/api/v1/")

    with app.app_context(), internal_api_base_url("https://thread.example.de/"):
        result = job(None)
    assert result["download_url"].startswith("https://thread.example.de/api/v1/downloads/")


class _StaticHttp:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        return ApiResponse(
            status=200,
            text=json.dumps(self.payload),
            content_type="application/json",
            json=self.payload,
        )


def _call_link_tool(server: MCPServer) -> dict:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_document_download_link", "arguments": {"document_id": 1}},
        }
    )
    return json.loads(response["result"]["content"][0]["text"])


def test_mcp_server_points_download_url_to_its_public_address():
    server = MCPServer(
        http=_StaticHttp(
            {
                "download_url": "http://app:8000/api/v1/downloads/abc",
                "download_path": "/api/v1/downloads/abc",
            }
        )
    )
    assert _call_link_tool(server)["download_url"] == "http://app:8000/api/v1/downloads/abc"
    with public_base_url_override("https://mkk.example.de/"):
        assert _call_link_tool(server)["download_url"] == (
            "https://mkk.example.de/api/v1/downloads/abc"
        )


@pytest.fixture
def live_app(tmp_path):
    app, _, document_id, file_bytes = _setup(tmp_path)
    httpd = make_wsgi_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", document_id, file_bytes
    finally:
        httpd.shutdown()


def test_mcp_http_server_builds_and_serves_download_links(live_app):
    app_url, document_id, file_bytes = live_app
    mcp = MCPServer(http=HttpApiClient(base_url=f"{app_url}/api/v1", token=GLOBAL_TOKEN))
    httpd = make_mcp_server(mcp, host="127.0.0.1", port=0, path="/mcp", auth_token=MCP_SECRET)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    mcp_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        request = Request(
            f"{mcp_url}/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "get_document_download_link",
                        "arguments": {"document_id": document_id},
                    },
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MCP_SECRET}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mkk.example.de",
            },
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
        download_url = json.loads(body["result"]["content"][0]["text"])["download_url"]
        assert download_url.startswith("https://mkk.example.de/api/v1/downloads/")

        # Zeigt die öffentliche Adresse nur auf den MCP-Server (Tailscale Serve, Caddy),
        # reicht er den Abruf an die App durch — ohne MCP-Token.
        with urlopen(f"{mcp_url}{urlsplit(download_url).path}", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/pdf"
            assert response.headers["Content-Disposition"].startswith("attachment")
            assert response.read() == file_bytes

        with pytest.raises(HTTPError) as excinfo:
            urlopen(f"{mcp_url}/api/v1/downloads/kaputt", timeout=5)
        assert excinfo.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
