"""Belege per Link (source_url): Abruf, Allowlist, SSRF-Schutz, API und MCP-Tools."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.services import mcp_server
from app.services.document_fetch import (
    AllowedHost,
    DocumentFetchError,
    fetch_document,
    parse_allowed_hosts,
)
from app.services.mcp_server import TOOLS_BY_NAME, HttpApiClient, MCPServer
from domain.models import AuditLog, Document
from tests.document_files import png_document_bytes
from tests.test_mcp_server import _TestClientHttp
from tests.test_receipt_ocr import (
    _create_test_app,
    _logged_in_client,
    _pdf_with_text,
    _seed_company_with_accounts,
)

PDF = _pdf_with_text(
    [
        "Muster Elektronik Ltd.",
        "Rechnung Nr. UA12345678",
        "Rechnungsdatum: 08.07.2026",
        "Nettobetrag 100,00 EUR",
        "MwSt 19 % 19,00 EUR",
        "Gesamtbetrag 119,00 EUR",
    ]
)
PDF_SHA256 = hashlib.sha256(PDF).hexdigest()
PNG = png_document_bytes(b"source-url")
MAX_BYTES = 64 * 1024


class _Handler(BaseHTTPRequestHandler):
    """Testserver in der Art des mcp-imap-Endpunkts GET /attachments/<token>/<name>."""

    protocol_version = "HTTP/1.0"  # Body endet mit Verbindungsende (ohne Content-Length)

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        if path == "/attachments/tok/UA12345678.pdf":
            self._send(
                200,
                PDF,
                {
                    "Content-Type": "application/pdf",
                    "Content-Length": str(len(PDF)),
                    "Content-Disposition": (
                        'attachment; filename="Rechnung.pdf"; '
                        f"filename*=UTF-8''{quote('Rechnung März.pdf')}"
                    ),
                },
            )
        elif path == "/files/scan.png":
            self._send(200, PNG, {"Content-Type": "application/octet-stream"})
        elif path == "/expired":
            self._send(404, b'{"error": "expired"}', {"Content-Type": "application/json"})
        elif path == "/boom":
            self._send(500, b"boom")
        elif path == "/html.pdf":
            body = b"<html>" + b"x" * 2048 + b"</html>"
            self._send(200, body, {"Content-Type": "application/pdf"})
        elif path == "/redirect-same":
            self._send(302, b"", {"Location": "/attachments/tok/UA12345678.pdf"})
        elif path == "/redirect-other":
            self._send(302, b"", {"Location": "http://example.com/attachments/x.pdf"})
        elif path == "/redirect-loop":
            self._send(302, b"", {"Location": "/redirect-loop"})
        elif path == "/big-declared":
            self._send(
                200,
                b"%PDF-1.4\n",
                {"Content-Type": "application/pdf", "Content-Length": str(MAX_BYTES * 10)},
            )
        elif path == "/big-stream":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"%PDF" + b"0" * 4092)
            except OSError:
                pass  # Client hat abgebrochen.
        elif path == "/slow":
            time.sleep(2)
            self._send(200, PDF, {"Content-Type": "application/pdf"})
        else:
            self._send(404, b"not found")

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def file_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _fetch(url: str, host: str, **kwargs):
    kwargs.setdefault("allowed_hosts", parse_allowed_hosts(host))
    kwargs.setdefault("max_bytes", MAX_BYTES)
    return fetch_document(url, **kwargs)


def _error_code(url: str, host: str, **kwargs) -> str:
    with pytest.raises(DocumentFetchError) as excinfo:
        _fetch(url, host, **kwargs)
    assert "content_base64" in str(excinfo.value) or excinfo.value.code == "too_large"
    return excinfo.value.code


# --- Allowlist ----------------------------------------------------------------------


def test_parse_allowed_hosts() -> None:
    assert parse_allowed_hosts(None) == ()
    assert parse_allowed_hosts(" ") == ()
    assert parse_allowed_hosts("Node.Tail.ts.net:10000, *.example.com, [::1]:8080") == (
        AllowedHost("node.tail.ts.net", 10000),
        AllowedHost("example.com", None, wildcard=True),
        AllowedHost("::1", 8080),
    )
    for raw in ("host:abc", "a.*.com:80", "host/path:80", ":80"):
        with pytest.raises(ValueError):
            parse_allowed_hosts(raw)


def test_invalid_allowlist_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DOCUMENT_FETCH_ALLOWED_HOSTS"):
        _create_test_app(tmp_path, DOCUMENT_FETCH_ALLOWED_HOSTS="host:abc")


# --- Abruf --------------------------------------------------------------------------


def test_fetch_derives_name_and_type_from_headers(file_server: str) -> None:
    fetched = _fetch(f"http://{file_server}/attachments/tok/UA12345678.pdf", file_server)
    assert fetched.content == PDF
    assert fetched.file_name == "Rechnung März.pdf"  # filename* gewinnt
    assert fetched.mime_type == "application/pdf"


def test_fetch_falls_back_to_url_path_and_extension(file_server: str) -> None:
    fetched = _fetch(f"http://{file_server}/files/scan.png", file_server)
    assert fetched.content == PNG
    assert fetched.file_name == "scan.png"
    assert fetched.mime_type == "image/png"


def test_fetch_follows_redirect_on_same_host_only(file_server: str) -> None:
    fetched = _fetch(f"http://{file_server}/redirect-same", file_server)
    assert fetched.content == PDF
    assert fetched.url.endswith("/attachments/tok/UA12345678.pdf")
    assert _error_code(f"http://{file_server}/redirect-other", file_server) == (
        "redirect_not_allowed"
    )
    assert _error_code(f"http://{file_server}/redirect-loop", file_server) == (
        "redirect_not_allowed"
    )


def test_fetch_reports_http_errors(file_server: str) -> None:
    with pytest.raises(DocumentFetchError) as excinfo:
        _fetch(f"http://{file_server}/expired", file_server)
    assert excinfo.value.code == "not_found"
    assert "abgelaufen" in str(excinfo.value)
    assert _error_code(f"http://{file_server}/boom", file_server) == "http_error"


def test_fetch_enforces_size_limit(file_server: str) -> None:
    assert _error_code(f"http://{file_server}/big-declared", file_server) == "too_large"
    assert _error_code(f"http://{file_server}/big-stream", file_server) == "too_large"


def test_fetch_enforces_timeout(file_server: str) -> None:
    started = time.monotonic()
    assert _error_code(f"http://{file_server}/slow", file_server, timeout=0.5) == "timeout"
    assert time.monotonic() - started < 1.9


def test_fetch_requires_configuration_and_allowed_host(file_server: str) -> None:
    url = f"http://{file_server}/attachments/tok/UA12345678.pdf"
    assert _error_code(url, file_server, allowed_hosts=()) == "disabled"
    # Anderer Port, anderer Host, Standardport ohne Eintrag:
    assert _error_code(url, "127.0.0.1:1") == "host_not_allowed"
    assert _error_code(url, f"localhost:{file_server.split(':')[1]}") == "host_not_allowed"
    assert _error_code("http://127.0.0.1/x.pdf", "127.0.0.1:8080") == "host_not_allowed"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://files.example.com/x.pdf",
        "file:///etc/passwd",
        "http://user:secret@files.example.com/x.pdf",
        "http://files.example.com:99999/x.pdf",
        "not a url",
    ],
)
def test_fetch_rejects_invalid_urls(url: str) -> None:
    assert _error_code(url, "files.example.com:80,files.example.com:21") == "invalid_url"


@pytest.mark.parametrize(
    ("allowed", "address", "expected"),
    [
        # Platzhalter erlauben keine internen Adressen …
        ("*.example.com:80", "10.0.0.5", "address_not_allowed"),
        ("*.example.com:80", "127.0.0.1", "address_not_allowed"),
        ("*.example.com:80", "100.101.102.103", "address_not_allowed"),  # Tailscale
        ("*.example.com:80", "fd00::1", "address_not_allowed"),
        # … Link-Local (Cloud-Metadaten) ist auch bei exaktem Eintrag gesperrt.
        ("files.example.com:80", "169.254.169.254", "address_not_allowed"),
        ("files.example.com:80", "::ffff:169.254.169.254", "address_not_allowed"),
        ("files.example.com:80", "0.0.0.0", "address_not_allowed"),
    ],
)
def test_fetch_blocks_internal_addresses(allowed: str, address: str, expected: str) -> None:
    def resolver(host: str, port: int) -> list[str]:
        return [address]

    url = "http://files.example.com/x.pdf"
    assert _error_code(url, allowed, resolver=resolver) == expected


def test_exact_entry_may_point_to_private_address(file_server: str) -> None:
    port = int(file_server.split(":")[1])
    seen: list[tuple[str, int]] = []

    def resolver(host: str, resolved_port: int) -> list[str]:
        seen.append((host, resolved_port))
        return ["127.0.0.1"]  # z. B. Tailscale-Host im selben Netz

    fetched = _fetch(
        f"http://mail-mcp.internal:{port}/attachments/tok/UA12345678.pdf",
        f"mail-mcp.internal:{port}",
        resolver=resolver,
    )
    assert fetched.content == PDF
    assert seen == [("mail-mcp.internal", port)]  # einmal aufgelöst, dann gepinnt


def test_unresolvable_host_is_reported() -> None:
    def resolver(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    url = "http://files.example.com/x.pdf"
    assert _error_code(url, "files.example.com:80", resolver=resolver) == "unreachable"


# --- REST-API -----------------------------------------------------------------------


def _setup(tmp_path: Path, allowed_hosts: str):
    app = _create_test_app(
        tmp_path,
        DOCUMENT_UPLOAD_DIR=str(tmp_path / "uploads"),
        DOCUMENT_FETCH_ALLOWED_HOSTS=allowed_hosts,
        DOCUMENT_MAX_UPLOAD_BYTES=MAX_BYTES,
    )
    company_id = _seed_company_with_accounts(app)
    return app, company_id, _logged_in_client(app)


def _documents(app) -> list[Document]:
    with app.extensions["db_session_factory"]() as session:
        return list(session.execute(select(Document).order_by(Document.id)).scalars())


def test_upload_document_from_source_url(tmp_path: Path, file_server: str) -> None:
    app, company_id, client = _setup(tmp_path, file_server)
    response = client.post(
        "/api/v1/documents",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/attachments/tok/UA12345678.pdf",
            "client_sha256": PDF_SHA256,
        },
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["file_name"] == "Rechnung_Marz.pdf"  # secure_filename
    assert body["mime_type"] == "application/pdf"
    assert body["file_sha256"] == PDF_SHA256
    assert body["duplicate"] is False
    stored = _documents(app)[0]
    assert Path(stored.storage_key).read_bytes() == PDF
    with app.extensions["db_session_factory"]() as session:
        audit = session.execute(select(AuditLog).where(AuditLog.action == "uploaded")).scalar_one()
    assert audit.payload["source_host"] == file_server
    assert audit.payload["client_hash_verified"] is True

    # Explizite Angaben haben Vorrang vor den abgeleiteten.
    response = client.post(
        "/api/v1/documents",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/files/scan.png",
            "file_name": "kassenbon.png",
            "mime_type": "image/png",
        },
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["file_name"] == "kassenbon.png"


def test_upload_document_source_url_errors(tmp_path: Path, file_server: str) -> None:
    app, company_id, client = _setup(tmp_path, file_server)

    def upload(**fields):
        return client.post(
            "/api/v1/documents",
            json={"company_id": company_id, "document_date": "2026-07-08", **fields},
        )

    url = f"http://{file_server}/attachments/tok/UA12345678.pdf"
    response = upload(source_url=url, content_base64="JVBERi0=")
    assert response.status_code == 400
    assert "exactly one" in response.get_json()["error"]

    response = upload()
    assert response.status_code == 400
    assert "source_url" in response.get_json()["error"]

    response = upload(source_url=f"http://{file_server}/expired")
    assert response.status_code == 502
    body = response.get_json()
    assert body["source_url_error"] == "not_found"
    assert body["fallback"] == "content_base64"

    response = upload(source_url="http://example.com/x.pdf")
    assert response.status_code == 400
    assert response.get_json()["source_url_error"] == "host_not_allowed"

    response = upload(source_url=url, client_sha256="0" * 64)
    assert response.status_code == 422
    assert "source_url" in response.get_json()["error"]

    response = upload(source_url=f"http://{file_server}/html.pdf")
    assert response.status_code == 422
    assert "does not match the declared file type" in response.get_json()["error"]

    response = upload(source_url=f"http://{file_server}/big-stream")
    assert response.status_code == 413
    assert response.get_json()["source_url_error"] == "too_large"

    assert _documents(app) == []


def test_source_url_disabled_without_configuration(tmp_path: Path, file_server: str) -> None:
    _app, company_id, client = _setup(tmp_path, "")
    response = client.post(
        "/api/v1/documents",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/attachments/tok/UA12345678.pdf",
        },
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["source_url_error"] == "disabled"
    assert "DOCUMENT_FETCH_ALLOWED_HOSTS" in body["error"]


def test_replace_document_from_source_url(tmp_path: Path, file_server: str) -> None:
    app, company_id, client = _setup(tmp_path, file_server)
    first = client.post(
        "/api/v1/documents",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/files/scan.png",
        },
    ).get_json()
    url = f"http://{file_server}/attachments/tok/UA12345678.pdf"

    response = client.post(
        f"/api/v1/documents/{first['id']}/replace",
        json={"source_url": url, "client_sha256": "0" * 64},
    )
    assert response.status_code == 422
    assert len(_documents(app)) == 1

    response = client.post(
        f"/api/v1/documents/{first['id']}/replace",
        json={"source_url": url, "client_sha256": PDF_SHA256},
    )
    assert response.status_code == 201, response.get_json()
    replacement = response.get_json()
    assert replacement["version_number"] == 2
    assert replacement["file_sha256"] == PDF_SHA256
    assert replacement["mime_type"] == "application/pdf"


def test_receipt_ocr_suggestion_from_source_url(tmp_path: Path, file_server: str) -> None:
    _app, company_id, client = _setup(tmp_path, file_server)
    response = client.post(
        "/api/v1/receipt-ocr/suggestions",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/attachments/tok/UA12345678.pdf",
            "client_sha256": PDF_SHA256,
        },
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["extraction"]["gross_amount"] == "119.00"

    response = client.post(
        "/api/v1/receipt-ocr/suggestions",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/expired",
        },
    )
    assert response.status_code == 502
    assert response.get_json()["source_url_error"] == "not_found"


def test_base64_upload_still_requires_name_and_type(tmp_path: Path, file_server: str) -> None:
    _app, company_id, client = _setup(tmp_path, file_server)
    response = client.post(
        "/api/v1/documents",
        json={
            "company_id": company_id,
            "document_date": "2026-07-08",
            "content_base64": "JVBERi0xLjQK",
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "file_name is required."


# --- MCP ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["upload_document", "replace_document", "create_receipt_ocr_suggestion"]
)
def test_document_tools_offer_source_url(name: str) -> None:
    tool = TOOLS_BY_NAME[name]
    schema = tool.input_schema
    assert "source_url" in schema["properties"]
    assert "client_sha256" in schema["properties"]
    for field in ("content_base64", "file_name", "mime_type", "source_url"):
        assert field not in schema["required"]
    assert "source_url bevorzugen" in tool.description
    assert tool.timeout is not None and tool.timeout > 30


def test_tool_timeout_reaches_http_client(monkeypatch) -> None:
    seen: list[float] = []

    class _Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    class _Headers(dict):
        def get_content_charset(self):
            return "utf-8"

    _Response.headers = _Headers({"Content-Type": "application/json"})

    def fake_urlopen(request, timeout):
        seen.append(timeout)
        return _Response()

    monkeypatch.setattr(mcp_server, "urlopen", fake_urlopen)
    server = MCPServer(http=HttpApiClient("http://example.test/api/v1"))
    for name, arguments in (
        ("upload_document", {"company_id": 1, "document_date": "2026-07-08"}),
        ("health", {}),
    ):
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    assert seen == [mcp_server.DOCUMENT_UPLOAD_TOOL_TIMEOUT, 15.0]


def test_mcp_upload_document_with_source_url(tmp_path: Path, file_server: str) -> None:
    app, company_id, _client = _setup(tmp_path, file_server)
    app.config["API_AUTH_TOKEN"] = "global-token"
    server = MCPServer(http=_TestClientHttp(app.test_client(), token="global-token"))

    def call_tool(arguments: dict) -> dict:
        return server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "upload_document", "arguments": arguments},
            }
        )["result"]

    result = call_tool(
        {
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/attachments/tok/UA12345678.pdf",
            "client_sha256": PDF_SHA256,
        }
    )
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"])["file_sha256"] == PDF_SHA256

    result = call_tool(
        {
            "company_id": company_id,
            "document_date": "2026-07-08",
            "source_url": f"http://{file_server}/expired",
        }
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "abgelaufen" in text
    assert "content_base64" in text
