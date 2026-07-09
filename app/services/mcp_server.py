"""MCP-Server, der die OpenBuchhaltung-REST-API (/api/v1) als Tools bereitstellt.

Der Server spricht das Model-Context-Protocol über stdio (zeilenweise
JSON-RPC 2.0) und leitet jeden Tool-Aufruf an den passenden HTTP-Endpunkt der
laufenden OpenBuchhaltung-Instanz weiter. Bewusst ohne externe Abhängigkeiten
(nur stdlib) – analog zum handgeschriebenen ``mcp_client``.

Start (die App muss unter der Basis-URL erreichbar sein)::

    OPENBUCHHALTUNG_API_URL=http://localhost:5000/api/v1 \\
    OPENBUCHHALTUNG_API_TOKEN=obk_... \\
    python -m app.services.mcp_server
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "openbuchhaltung"
SERVER_VERSION = "1.0.0"
DEFAULT_API_URL = "http://localhost:5000/api/v1"


class MCPTransportError(Exception):
    """Die OpenBuchhaltung-API ist nicht erreichbar oder liefert kein JSON."""


@dataclass(slots=True)
class ApiResponse:
    status: int
    text: str
    content_type: str = ""
    json: Any = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    http_method: str
    path: str
    arg_location: str  # "query" | "json" | "none"

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _company_id_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "company_id": {
                "type": "integer",
                "description": "ID der Gesellschaft.",
            }
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


def _period_report_schema() -> dict[str, Any]:
    """Schema für Zeitraum-Reports (GuV, Summen-/Saldenliste)."""
    return {
        "type": "object",
        "properties": {
            "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            "date_from": {
                "type": "string",
                "description": "Zeitraum-Beginn (Format JJJJ-MM-TT, optional).",
            },
            "date_to": {
                "type": "string",
                "description": "Zeitraum-Ende einschließlich (Format JJJJ-MM-TT, optional).",
            },
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


def _asof_report_schema() -> dict[str, Any]:
    """Schema für Stichtags-Reports (Bilanz)."""
    return {
        "type": "object",
        "properties": {
            "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            "date_to": {
                "type": "string",
                "description": "Stichtag: Buchungen bis einschließlich (JJJJ-MM-TT, optional).",
            },
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


# Ein Tool je REST-Endpunkt unter /api/v1 (ohne den rekursiven /mcp/call-Proxy).
TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="health",
        description="Prüft, ob die OpenBuchhaltung-API erreichbar ist.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/health",
        arg_location="none",
    ),
    ToolSpec(
        name="list_companies",
        description="Listet alle Gesellschaften im Zugriffsbereich des API-Tokens.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/companies",
        arg_location="none",
    ),
    ToolSpec(
        name="create_tenant_with_company",
        description="Legt einen neuen Mandanten samt erster Gesellschaft an.",
        input_schema={
            "type": "object",
            "properties": {
                "tenant_name": {"type": "string", "description": "Name des Mandanten."},
                "company_name": {"type": "string", "description": "Name der Gesellschaft."},
                "currency_code": {
                    "type": "string",
                    "description": "ISO-Währungscode, Standard EUR.",
                    "default": "EUR",
                },
            },
            "required": ["tenant_name", "company_name"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/tenants",
        arg_location="json",
    ),
    ToolSpec(
        name="create_account",
        description="Legt ein Sachkonto für eine Gesellschaft an.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "code": {"type": "string", "description": "Kontonummer (z. B. 1200)."},
                "name": {"type": "string", "description": "Kontobezeichnung."},
                "account_type": {
                    "type": "string",
                    "description": "Kontoart, z. B. asset, income, expense, equity, liability.",
                },
            },
            "required": ["company_id", "code", "name", "account_type"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/accounts",
        arg_location="json",
    ),
    ToolSpec(
        name="list_accounts",
        description=(
            "Listet die Sachkonten einer Gesellschaft mit interner ID, Kontonummer (code), "
            "Bezeichnung und Kontoart. Nützlich, um Kontonummern zu finden bzw. IDs "
            "aufzulösen. Standardmäßig nur aktive Konten; include_inactive=true zeigt alle."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch inaktive Konten einschließen (Standard: false).",
                    "default": False,
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/accounts",
        arg_location="query",
    ),
    ToolSpec(
        name="create_journal_entry",
        description=(
            "Erfasst eine Buchung mit mindestens zwei Zeilen. Soll- und Haben-Summe "
            "müssen ausgeglichen sein. Das Konto je Zeile wird entweder über die interne "
            "'account_id' ODER über die Kontonummer 'account_code' (z. B. '1200') angegeben "
            "– die Kontonummer wird serverseitig zur ID aufgelöst."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "entry_date": {
                    "type": "string",
                    "description": "Belegdatum im Format JJJJ-MM-TT.",
                },
                "description": {"type": "string", "description": "Buchungstext."},
                "status": {
                    "type": "string",
                    "description": "Buchungsstatus, Standard 'posted'.",
                    "default": "posted",
                },
                "lines": {
                    "type": "array",
                    "description": "Buchungszeilen.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_id": {
                                "type": "integer",
                                "description": (
                                    "Interne Konto-ID. Alternativ 'account_code' angeben."
                                ),
                            },
                            "account_code": {
                                "type": "string",
                                "description": (
                                    "Kontonummer (z. B. '1200'). Alternative zu 'account_id'; "
                                    "genau eines von beiden angeben."
                                ),
                            },
                            "debit_amount": {
                                "type": "string",
                                "description": "Sollbetrag als Dezimalzahl, z. B. '100.00'.",
                            },
                            "credit_amount": {
                                "type": "string",
                                "description": "Habenbetrag als Dezimalzahl, z. B. '100.00'.",
                            },
                            "description": {"type": "string"},
                            "tax_code_id": {"type": "integer"},
                        },
                        "anyOf": [
                            {"required": ["account_id"]},
                            {"required": ["account_code"]},
                        ],
                        "additionalProperties": False,
                    },
                    "minItems": 2,
                },
            },
            "required": ["company_id", "entry_date", "description", "lines"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-entries",
        arg_location="json",
    ),
    ToolSpec(
        name="get_trial_balance",
        description=(
            "Liefert die Summen- und Saldenliste einer Gesellschaft, optional für einen "
            "Zeitraum (date_from/date_to, Format JJJJ-MM-TT)."
        ),
        input_schema=_period_report_schema(),
        http_method="GET",
        path="/trial-balance",
        arg_location="query",
    ),
    ToolSpec(
        name="get_income_statement",
        description=(
            "Liefert die Gewinn- und Verlustrechnung (GuV) einer Gesellschaft, optional für "
            "einen Zeitraum (date_from/date_to, Format JJJJ-MM-TT). Ohne Angabe: alle Buchungen. "
            "Der ausgewertete Zeitraum steht im Feld 'period' der Antwort."
        ),
        input_schema=_period_report_schema(),
        http_method="GET",
        path="/income-statement",
        arg_location="query",
    ),
    ToolSpec(
        name="get_balance_sheet",
        description=(
            "Liefert die Bilanz einer Gesellschaft zum Stichtag date_to (Format JJJJ-MM-TT, "
            "optional; ohne Angabe alle Buchungen). Der Stichtag steht im Feld 'period.as_of'."
        ),
        input_schema=_asof_report_schema(),
        http_method="GET",
        path="/balance-sheet",
        arg_location="query",
    ),
    ToolSpec(
        name="export_trial_balance_csv",
        description="Exportiert die Summen- und Saldenliste als CSV.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/exports/trial-balance.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_journal_csv",
        description="Exportiert das Buchungsjournal als CSV.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/exports/journal.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_datev_csv",
        description="Exportiert den DATEV-Buchungsstapel (EXTF) als CSV.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/exports/datev.csv",
        arg_location="query",
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


class HttpApiClient:
    """Ruft die OpenBuchhaltung-REST-API über HTTP auf (stdlib urllib)."""

    def __init__(self, base_url: str, token: str | None = None, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ApiResponse:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
                status = response.status
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            status = exc.code
        except URLError as exc:
            raise MCPTransportError(
                f"OpenBuchhaltung-API nicht erreichbar unter {self.base_url}: {exc.reason}"
            ) from exc

        parsed = None
        if "application/json" in content_type:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
        return ApiResponse(status=status, text=body, content_type=content_type, json=parsed)


@dataclass(slots=True)
class MCPServer:
    """Übersetzt MCP-JSON-RPC-Nachrichten in API-Aufrufe."""

    http: HttpApiClient
    protocol_version: str = PROTOCOL_VERSION

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Verarbeitet eine JSON-RPC-Nachricht; gibt None für Notifications zurück."""
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        # Notifications (ohne id) werden nicht beantwortet.
        if message_id is None and method and method.startswith("notifications/"):
            return None

        try:
            result = self._dispatch(method, params)
        except _JsonRpcError as exc:
            return {"jsonrpc": "2.0", "id": message_id, "error": exc.to_dict()}

        if message_id is None:
            return None
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            if isinstance(requested, str) and requested:
                self.protocol_version = requested
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.to_mcp() for tool in TOOLS]}
        if method == "tools/call":
            return self._call_tool(params)
        raise _JsonRpcError(-32601, f"Unbekannte Methode: {method}")

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise _JsonRpcError(-32602, f"Unbekanntes Tool: {name}")
        if not isinstance(arguments, dict):
            raise _JsonRpcError(-32602, "arguments muss ein Objekt sein.")

        try:
            response = self._request_for_tool(tool, arguments)
        except MCPTransportError as exc:
            return _tool_result(str(exc), is_error=True)

        if response.json is not None:
            text = json.dumps(response.json, ensure_ascii=False, indent=2)
        else:
            text = response.text
        return _tool_result(text, is_error=not response.ok)

    def _request_for_tool(self, tool: ToolSpec, arguments: dict[str, Any]) -> ApiResponse:
        if tool.arg_location == "query":
            return self.http.call(tool.http_method, tool.path, params=arguments)
        if tool.arg_location == "json":
            return self.http.call(tool.http_method, tool.path, json_body=arguments)
        return self.http.call(tool.http_method, tool.path)


@dataclass(slots=True)
class _JsonRpcError(Exception):
    code: int
    message: str
    data: Any = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def build_server_from_env(getenv: Callable[[str], str | None] = os.environ.get) -> MCPServer:
    base_url = getenv("OPENBUCHHALTUNG_API_URL") or DEFAULT_API_URL
    token = getenv("OPENBUCHHALTUNG_API_TOKEN")
    return MCPServer(http=HttpApiClient(base_url=base_url, token=token))


def serve(server: MCPServer, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Liest zeilenweise JSON-RPC von stdin und schreibt Antworten nach stdout."""
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
            continue

        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    serve(build_server_from_env())


if __name__ == "__main__":
    main()
