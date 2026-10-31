"""Wissensbasis (Fachwissen für Obi) über die API nachschlagen."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_scoped_company, get_session_factory
from app.services.company_knowledge import search_with_company_knowledge
from app.services.knowledge import DEFAULT_SEARCH_LIMIT, search_knowledge


@api_bp.get("/knowledge")
def knowledge_search():
    """Sucht Abschnitte der Wissensbasis (``q``/``query``); ohne Suchbegriff das Inhalts-
    verzeichnis.

    Die allgemeine Wissensbasis ist für alle Benutzer gleich. Mit ``company_id`` kommen
    die Treffer im Firmenwissen dieser Gesellschaft hinzu (``company_results``).
    """
    query = request.args.get("query") or request.args.get("q") or ""
    limit = request.args.get("limit", type=int) or DEFAULT_SEARCH_LIMIT
    company_id = request.args.get("company_id", type=int)
    if company_id is None:
        return jsonify(search_knowledge(query, limit=limit))
    with get_session_factory()() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        return jsonify(
            search_with_company_knowledge(
                session=session, company_id=company_id, query=query, limit=limit
            )
        )
