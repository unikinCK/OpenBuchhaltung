from __future__ import annotations

import io
import json
from pathlib import Path

from app import create_app
from app.services.mcp_server import (
    TOOLS,
    ApiResponse,
    MCPServer,
    build_server_from_env,
    serve,
)

EXPECTED_TOOL_NAMES = {
    "health",
    "list_companies",
    "create_tenant_with_company",
    "create_account",
    "list_accounts",
    "create_journal_entry",
    "finalize_journal_entry",
    "reverse_journal_entry",
    "get_vat_return",
    "list_vat_returns",
    "create_vat_return",
    "get_trial_balance",
    "get_income_statement",
    "get_balance_sheet",
    "export_trial_balance_csv",
    "export_journal_csv",
    "export_datev_csv",
    "create_fixed_asset",
    "list_fixed_assets",
    "get_depreciation_schedule",
    "post_depreciation",
}


class RecordingHttp:
    """Fake-HTTP-Client, der Aufrufe protokolliert und vorgegebene Antworten liefert."""

    def __init__(self, response: ApiResponse | None = None) -> None:
        self.calls: list[tuple] = []
        self.response = response or ApiResponse(
            status=200, text='{"ok": true}', content_type="application/json", json={"ok": True}
        )

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        self.calls.append((method, path, params, json_body))
        return self.response


def test_tools_cover_all_api_endpoints() -> None:
    assert {tool.name for tool in TOOLS} == EXPECTED_TOOL_NAMES
    # Jedes Tool trägt ein gültiges JSON-Schema und einen HTTP-Verweis.
    for tool in TOOLS:
        assert tool.input_schema["type"] == "object"
        assert tool.http_method in {"GET", "POST"}
        assert tool.path.startswith("/")


def test_initialize_echoes_requested_protocol_version() -> None:
    server = MCPServer(http=RecordingHttp())
    result = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "openbuchhaltung"
    assert "tools" in result["capabilities"]


def test_tools_list_returns_all_tools() -> None:
    server = MCPServer(http=RecordingHttp())
    result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
    assert {tool["name"] for tool in result["tools"]} == EXPECTED_TOOL_NAMES


def test_query_tool_forwards_arguments_as_query_params() -> None:
    http = RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_balance_sheet", "arguments": {"company_id": 7}},
        }
    )
    assert http.calls[-1] == ("GET", "/balance-sheet", {"company_id": 7}, None)


def test_path_placeholder_filled_from_arguments() -> None:
    http = RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "post_depreciation",
                "arguments": {"asset_id": 5, "fiscal_year": 2026},
            },
        }
    )
    # asset_id landet im Pfad, nicht im Body.
    assert http.calls[-1] == (
        "POST",
        "/fixed-assets/5/depreciation",
        None,
        {"fiscal_year": 2026},
    )


def test_finalize_and_reverse_fill_path_placeholder() -> None:
    http = RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "finalize_journal_entry",
                "arguments": {"journal_entry_id": 9},
            },
        }
    )
    assert http.calls[-1] == ("POST", "/journal-entries/9/finalize", None, {})

    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "reverse_journal_entry",
                "arguments": {"journal_entry_id": 9, "reversal_date": "2026-07-12"},
            },
        }
    )
    assert http.calls[-1] == (
        "POST",
        "/journal-entries/9/reverse",
        None,
        {"reversal_date": "2026-07-12"},
    )


def test_vat_return_tools_forward_arguments() -> None:
    http = RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {
                "name": "get_vat_return",
                "arguments": {"company_id": 7, "period": "2026-H1"},
            },
        }
    )
    assert http.calls[-1] == ("GET", "/vat-return", {"company_id": 7, "period": "2026-H1"}, None)

    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 34,
            "method": "tools/call",
            "params": {
                "name": "create_vat_return",
                "arguments": {"company_id": 7, "period": "2026-Q2"},
            },
        }
    )
    assert http.calls[-1] == ("POST", "/vat-returns", None, {"company_id": 7, "period": "2026-Q2"})

    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 35,
            "method": "tools/call",
            "params": {"name": "list_vat_returns", "arguments": {"company_id": 7}},
        }
    )
    assert http.calls[-1] == ("GET", "/vat-returns", {"company_id": 7}, None)


def test_income_statement_forwards_date_range_as_query() -> None:
    http = RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "get_income_statement",
                "arguments": {
                    "company_id": 1,
                    "date_from": "2026-01-01",
                    "date_to": "2026-03-31",
                },
            },
        }
    )
    assert http.calls[-1] == (
        "GET",
        "/income-statement",
        {"company_id": 1, "date_from": "2026-01-01", "date_to": "2026-03-31"},
        None,
    )


def test_report_tools_expose_date_parameters() -> None:
    by_name = {tool.name: tool for tool in TOOLS}
    period_props = by_name["get_income_statement"].input_schema["properties"]
    assert "date_from" in period_props and "date_to" in period_props
    assert "date_from" in by_name["get_trial_balance"].input_schema["properties"]
    # Bilanz ist Stichtag: date_to, aber kein date_from.
    balance_props = by_name["get_balance_sheet"].input_schema["properties"]
    assert "date_to" in balance_props and "date_from" not in balance_props


def test_json_tool_forwards_arguments_as_body() -> None:
    http = RecordingHttp(
        ApiResponse(status=201, text='{"id": 1}', content_type="application/json", json={"id": 1})
    )
    server = MCPServer(http=http)
    payload = {"tenant_name": "M", "company_name": "C"}
    result = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "create_tenant_with_company", "arguments": payload},
        }
    )["result"]
    assert http.calls[-1] == ("POST", "/tenants", None, payload)
    assert result["isError"] is False


def test_error_status_marks_tool_result_as_error() -> None:
    http = RecordingHttp(
        ApiResponse(
            status=404,
            text='{"error": "Company not found."}',
            content_type="application/json",
            json={"error": "Company not found."},
        )
    )
    server = MCPServer(http=http)
    result = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "get_trial_balance", "arguments": {"company_id": 999}},
        }
    )["result"]
    assert result["isError"] is True
    assert "Company not found" in result["content"][0]["text"]


def test_unknown_tool_returns_jsonrpc_error() -> None:
    server = MCPServer(http=RecordingHttp())
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "does_not_exist", "arguments": {}},
        }
    )
    assert response["error"]["code"] == -32602


def test_unknown_method_returns_jsonrpc_error() -> None:
    server = MCPServer(http=RecordingHttp())
    response = server.handle({"jsonrpc": "2.0", "id": 7, "method": "totally/unknown"})
    assert response["error"]["code"] == -32601


def test_notifications_are_not_answered() -> None:
    server = MCPServer(http=RecordingHttp())
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_serve_processes_lines_and_skips_notifications() -> None:
    server = MCPServer(http=RecordingHttp())
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                "not-json",
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    serve(server, stdin=stdin, stdout=stdout)

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    # tools/list-Antwort + Parse-Error, aber keine Antwort auf die Notification.
    assert len(lines) == 2
    assert lines[0]["id"] == 1
    assert lines[1]["error"]["code"] == -32700


def test_build_server_from_env_uses_configured_url_and_token() -> None:
    env = {
        "OPENBUCHHALTUNG_API_URL": "http://example.test/api/v1/",
        "OPENBUCHHALTUNG_API_TOKEN": "obk_secret",
    }
    server = build_server_from_env(getenv=env.get)
    assert server.http.base_url == "http://example.test/api/v1"
    assert server.http.token == "obk_secret"


class _TestClientHttp:
    """Adapter, der die MCP-HTTP-Aufrufe auf einen Flask-Test-Client abbildet."""

    def __init__(self, client, token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        url = f"/api/v1{path}"
        if method == "GET":
            response = self.client.get(url, query_string=params or {}, headers=self.headers)
        else:
            response = self.client.post(url, json=json_body or {}, headers=self.headers)
        body = response.get_data(as_text=True)
        content_type = response.headers.get("Content-Type", "")
        parsed = response.get_json(silent=True) if "application/json" in content_type else None
        return ApiResponse(
            status=response.status_code, text=body, content_type=content_type, json=parsed
        )


def test_mcp_tools_run_against_live_api(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_mcp.db'}",
            "API_AUTH_TOKEN": "global-token",
        }
    )
    client = app.test_client()
    server = MCPServer(http=_TestClientHttp(client, token="global-token"))

    def call_tool(name: str, arguments: dict) -> dict:
        return server.handle(
            {
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )["result"]

    health = call_tool("health", {})
    assert health["isError"] is False

    created = call_tool(
        "create_tenant_with_company",
        {"tenant_name": "MCP Mandant", "company_name": "MCP GmbH"},
    )
    assert created["isError"] is False
    company_id = json.loads(created["content"][0]["text"])["company"]["id"]

    for code, name, account_type in [
        ("1200", "Bank", "asset"),
        ("8400", "Erlöse", "income"),
    ]:
        result = call_tool(
            "create_account",
            {"company_id": company_id, "code": code, "name": name, "account_type": account_type},
        )
        assert result["isError"] is False

    companies = json.loads(call_tool("list_companies", {})["content"][0]["text"])
    # Konten werden der Reihe nach angelegt, daher sind die IDs 1 (Bank) und 2 (Erlöse).
    bank_id, revenue_id = 1, 2

    entry = call_tool(
        "create_journal_entry",
        {
            "company_id": company_id,
            "entry_date": "2026-03-15",
            "description": "MCP Buchung",
            "lines": [
                {"account_id": bank_id, "debit_amount": "100.00"},
                {"account_id": revenue_id, "credit_amount": "100.00"},
            ],
        },
    )
    assert entry["isError"] is False

    tb_result = call_tool("get_trial_balance", {"company_id": company_id})
    trial_balance = json.loads(tb_result["content"][0]["text"])
    assert trial_balance["company_id"] == company_id
    assert any(row["code"] == "1200" for row in trial_balance["rows"])

    csv_result = call_tool("export_journal_csv", {"company_id": company_id})
    assert csv_result["isError"] is False
    assert "posting_number" in csv_result["content"][0]["text"]
    assert company_id == companies[0]["id"]

    # GoBD: Festschreiben und Storno über MCP.
    entry_id = json.loads(entry["content"][0]["text"])["id"]
    finalized = call_tool("finalize_journal_entry", {"journal_entry_id": entry_id})
    assert finalized["isError"] is False
    assert json.loads(finalized["content"][0]["text"])["is_finalized"] is True

    reversed_result = call_tool(
        "reverse_journal_entry",
        {"journal_entry_id": entry_id, "reversal_date": "2026-03-20"},
    )
    assert reversed_result["isError"] is False
    reversal = json.loads(reversed_result["content"][0]["text"])
    assert reversal["reversal_of_id"] == entry_id

    # Doppelstorno wird von der API abgewiesen und als Tool-Fehler gemeldet.
    second = call_tool(
        "reverse_journal_entry",
        {"journal_entry_id": entry_id, "reversal_date": "2026-03-21"},
    )
    assert second["isError"] is True

    # UStVA: berechnen, festhalten, auflisten.
    vat = json.loads(
        call_tool("get_vat_return", {"company_id": company_id, "period": "2026-03"})[
            "content"
        ][0]["text"]
    )
    assert vat["period"] == "2026-03"
    assert {row["kennziffer"] for row in vat["kennzahlen"]} >= {"81", "66", "83"}

    saved = call_tool("create_vat_return", {"company_id": company_id, "period": "2026-03"})
    assert saved["isError"] is False
    assert json.loads(saved["content"][0]["text"])["period_label"] == "2026-03"

    listed = json.loads(
        call_tool("list_vat_returns", {"company_id": company_id})["content"][0]["text"]
    )
    assert [item["period_label"] for item in listed["vat_returns"]] == ["2026-03"]
