"""Proxy-Endpunkt für MCP-Server-Aufrufe."""

from __future__ import annotations

from flask import current_app, jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, forbidden
from app.services.mcp_client import MCPError, call_mcp_server


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

    server_url = current_app.config.get("MCP_SERVER_URL")
    try:
        mcp_response = call_mcp_server(
            server_url=server_url,
            method=method,
            params=params,
            request_id=request_id,
        )
    except MCPError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    return jsonify(mcp_response), 200
