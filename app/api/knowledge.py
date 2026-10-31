"""Wissensbasis (Fachwissen für Obi) über die API nachschlagen."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_scoped_company, get_session_factory, mcp_channel
from app.services.company_knowledge import search_with_company_knowledge
from app.services.knowledge import DEFAULT_SEARCH_LIMIT, search_knowledge


@api_bp.get("/knowledge")
def knowledge_search():
    """Sucht Abschnitte der Wissensbasis (``q``/``query``); ohne Suchbegriff das Inhalts-
    verzeichnis.

    Die allgemeine Wissensbasis ist für alle Benutzer gleich. Mit ``company_id`` kommen
    die Treffer im Firmenwissen dieser Gesellschaft hinzu (``company_results``) – über
    den MCP-Server nur, wenn die Gesellschaft ihr Firmenwissen dafür freigegeben hat.
    """
    query = request.args.get("query") or request.args.get("q") or ""
    limit = request.args.get("limit", type=int) or DEFAULT_SEARCH_LIMIT
    company_id = request.args.get("company_id", type=int)
    if company_id is None:
        return jsonify(search_knowledge(query, limit=limit))
    with get_session_factory()() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        if mcp_channel() and not company.knowledge_mcp_enabled:
            result = search_knowledge(query, limit=limit)
            result["company"] = {
                "company_id": company_id,
                "note": "Das Firmenwissen dieser Gesellschaft ist für MCP-Clients nicht "
                "freigegeben.",
            }
            result["company_results"] = []
            return jsonify(result)
        return jsonify(
            search_with_company_knowledge(
                session=session, company_id=company_id, query=query, limit=limit
            )
        )
