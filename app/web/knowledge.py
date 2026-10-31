"""Wissensbasis (Fachwissen für Obi) in der Oberfläche durchsuchen."""

from __future__ import annotations

from flask import render_template, request

from app.services.company_knowledge import search_with_company_knowledge
from app.services.knowledge import DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT, search_knowledge
from app.web.blueprint import main_bp
from app.web.helpers import company_context, get_session_factory


@main_bp.get("/wissen")
def knowledge_page():
    query = (request.args.get("q") or "").strip()
    limit = request.args.get("limit", type=int) or DEFAULT_SEARCH_LIMIT
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    with get_session_factory()() as session:
        companies, selected_company_id = company_context(session)
        if selected_company_id:
            result = search_with_company_knowledge(
                session=session, company_id=selected_company_id, query=query, limit=limit
            )
        else:
            result = search_knowledge(query, limit=limit)
    return render_template(
        "wissen.html",
        companies=companies,
        selected_company_id=selected_company_id,
        query=query,
        limit=limit,
        rechtsstand=result["rechtsstand"],
        results=result["results"],
        toc=result["toc"],
        company_summary=result.get("company"),
        company_results=result.get("company_results", []),
    )
