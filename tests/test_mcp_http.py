from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.services.mcp_http import (
    accepts_event_stream,
    make_server,
    origin_allowed,
    process_post,
)
from app.services.mcp_server import ApiResponse, MCPServer


class RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        self.calls.append((method, path, params, json_body))
        return ApiResponse(
            status=200, text='{"ok": true}', content_type="application/json", json={"ok": True}
        )


def _server() -> MCPServer:
    return MCPServer(http=RecordingHttp())


def _tools_list(msg_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "tools/list"})


def test_process_post_returns_json_by_default() -> None:
    result = process_post(_server(), _tools_list(), accept_header="application/json")
    assert result.status == 200
    assert result.content_type == "application/json"
    body = json.loads(result.body)
    assert body["id"] == 1
    assert len(body["result"]["tools"]) == 11


def test_process_post_returns_sse_when_requested() -> None:
    result = process_post(_server(), _tools_list(), accept_header="text/event-stream")
    assert result.status == 200
    assert result.content_type == "text/event-stream"
    assert result.body.startswith("event: message\ndata: ")
    # Der SSE-Datenblock enthält gültiges JSON-RPC.
    data_line = next(
        line[len("data: ") :] for line in result.body.splitlines() if line.startswith("data: ")
    )
    assert json.loads(data_line)["id"] == 1


def test_process_post_notification_yields_202_without_body() -> None:
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    result = process_post(_server(), notification, accept_header="application/json")
    assert result.status == 202
    assert result.body == ""


def test_process_post_handles_batch() -> None:
    batch = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
    )
    result = process_post(_server(), batch, accept_header="application/json")
    payload = json.loads(result.body)
    assert isinstance(payload, list)
    assert {item["id"] for item in payload} == {1, 2}


def test_process_post_rejects_invalid_json() -> None:
    result = process_post(_server(), "not-json", accept_header="application/json")
    assert result.status == 400
    assert json.loads(result.body)["error"]["code"] == -32700


def test_accepts_event_stream_helper() -> None:
    assert accepts_event_stream("application/json, text/event-stream") is True
    assert accepts_event_stream("application/json") is False
    assert accepts_event_stream(None) is False


def test_origin_allowed() -> None:
    allowed = frozenset({"http://localhost:3000"})
    assert origin_allowed(None, allowed) is True  # Nicht-Browser-Clients
    assert origin_allowed("http://localhost:3000", allowed) is True
    assert origin_allowed("http://evil.example", allowed) is False
    assert origin_allowed("http://evil.example", frozenset()) is False


def _run_server(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def test_http_server_end_to_end() -> None:
    httpd = make_server(_server(), host="127.0.0.1", port=0, path="/mcp")
    _run_server(httpd)
    try:
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"

        # JSON-Antwort auf tools/list.
        request = Request(
            f"{base}/mcp",
            data=_tools_list().encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert "application/json" in response.headers.get("Content-Type", "")
            body = json.loads(response.read())
        assert len(body["result"]["tools"]) == 11

        # SSE-Antwort, wenn text/event-stream akzeptiert wird.
        sse_request = Request(
            f"{base}/mcp",
            data=_tools_list(2).encode(),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        with urlopen(sse_request, timeout=5) as response:
            assert "text/event-stream" in response.headers.get("Content-Type", "")
            text = response.read().decode()
        assert "event: message" in text and "data: " in text

        # GET wird nicht unterstützt.
        try:
            urlopen(Request(f"{base}/mcp", method="GET"), timeout=5)
            raise AssertionError("GET should not be allowed")
        except HTTPError as exc:
            assert exc.code == 405

        # Unbekannter Pfad -> 404.
        try:
            urlopen(Request(f"{base}/other", data=b"{}", method="POST"), timeout=5)
            raise AssertionError("unknown path should 404")
        except HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_server_rejects_disallowed_origin() -> None:
    httpd = make_server(
        _server(),
        host="127.0.0.1",
        port=0,
        path="/mcp",
        allowed_origins=frozenset({"http://localhost:3000"}),
    )
    _run_server(httpd)
    try:
        port = httpd.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/mcp",
            data=_tools_list().encode(),
            headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
            method="POST",
        )
        try:
            urlopen(request, timeout=5)
            raise AssertionError("disallowed origin should be rejected")
        except HTTPError as exc:
            assert exc.code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
