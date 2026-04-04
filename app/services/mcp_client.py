from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(slots=True)
class MCPError(Exception):
    message: str
    status_code: int = 502


def call_mcp_server(server_url: str | None, method: str, params: dict, request_id: str | int):
    if not server_url:
        raise MCPError("MCP server URL is not configured.", status_code=503)

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        server_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body)
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise MCPError(
            f"MCP server returned HTTP {exc.code}: {error_body}",
            status_code=502,
        ) from exc
    except URLError as exc:
        raise MCPError("MCP server is not reachable.", status_code=502) from exc
    except json.JSONDecodeError as exc:
        raise MCPError("MCP server response is not valid JSON.", status_code=502) from exc
