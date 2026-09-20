"""JSON-RPC-Bridge auf den MCP-Server (/api/v1/mcp/call), in-process.

Die Bridge führt MCP-Nachrichten (``initialize``, ``tools/list``, ``tools/call``)
direkt in-process aus: Jeder Tool-Aufruf landet über den
:class:`~app.services.internal_api.InProcessApiClient` in der eigenen REST-API,
und zwar mit dem Auth-Kontext des Aufrufers. Ein Benutzer-Token sieht und ändert
damit genau das, was es auch per REST dürfte — es gibt keinen externen
MCP-Server mit globalem Token mehr, der Tenant-Scoping oder Rollen aushebeln
könnte.
"""

from __future__ import annotations

from flask import current_app, jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, forbidden
from app.auth import api_has_global_access, current_api_user
from app.services.internal_api import InProcessApiClient
from app.services.mcp_server import MCPServer


@api_bp.post("/mcp/call")
def mcp_call():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    method = (payload.get("method") or "").strip()
    params = payload.get("params") or {}
    request_id = payload.get("id", "openbuchhaltung-mcp-call")

    if not method:
        return jsonify({"error": "method is required."}), 400

    if not isinstance(params, dict):
        return jsonify({"error": "params must be an object."}), 400

    server = MCPServer(
        http=InProcessApiClient(
            current_app._get_current_object(),
            api_user=current_api_user(),
            global_access=api_has_global_access(),
        )
    )
    response = server.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    )
    if response is None:
        # Notification ohne Antwort — trotzdem eine gültige JSON-Hülle liefern.
        response = {"jsonrpc": "2.0", "id": request_id, "result": None}
    return jsonify(response), 200
