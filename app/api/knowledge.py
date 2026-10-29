"""Wissensbasis (Fachwissen für Obi) über die API nachschlagen."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.services.knowledge import DEFAULT_SEARCH_LIMIT, search_knowledge


@api_bp.get("/knowledge")
def knowledge_search():
    """Sucht Abschnitte der Wissensbasis (``q``/``query``); ohne Suchbegriff das Inhalts-
    verzeichnis.

    Lesend, kein Mandantenbezug: Die Wissensbasis ist für alle Benutzer gleich.
    """
    query = request.args.get("query") or request.args.get("q") or ""
    limit = request.args.get("limit", type=int) or DEFAULT_SEARCH_LIMIT
    return jsonify(search_knowledge(query, limit=limit))
