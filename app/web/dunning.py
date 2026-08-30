"""Mahnwesen: Mahnvorschlagsliste, Mahnstufen und druckbares Mahnschreiben."""

from __future__ import annotations

from datetime import date, timedelta

from flask import abort, flash, redirect, render_template, url_for

from app.services.dunning import (
    DUNNING_LEVEL_LABELS,
    DunningError,
    dunning_proposals,
    record_dunning,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import OpenItem

# Zahlungsziel im Mahnschreiben.
PAYMENT_DEADLINE_DAYS = 10


@main_bp.get("/mahnwesen")
def dunning_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        proposals = []
        if selected_company_id:
            proposals = dunning_proposals(session=session, company_id=selected_company_id)

    return render_template(
        "mahnwesen.html",
        companies=companies,
        selected_company_id=selected_company_id,
        proposals=proposals,
        level_labels=DUNNING_LEVEL_LABELS,
    )


@main_bp.post("/mahnwesen/<int:open_item_id>/mahnen")
def dunning_record_action(open_item_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        item = session.get(OpenItem, open_item_id)
        if item is None:
            abort(404)
        require_company_access(session, item.company_id)
        company_id = item.company_id
        try:
            item = record_dunning(
                session=session, open_item_id=open_item_id, changed_by=changed_by()
            )
        except DunningError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.dunning_page", company_id=company_id))

    flash(
        f"Posten {item.reference} auf Mahnstufe {item.dunning_level} "
        f"({DUNNING_LEVEL_LABELS[item.dunning_level]}) gesetzt.",
        "success",
    )
    return redirect(url_for("main.dunning_page", company_id=company_id))


@main_bp.get("/mahnwesen/<int:open_item_id>/schreiben")
def dunning_letter_page(open_item_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        item = session.get(OpenItem, open_item_id)
        if item is None:
            abort(404)
        company = require_company_access(session, item.company_id)
        if item.item_type != "receivable":
            abort(404)
        level = max(1, min(item.dunning_level + 1, 3)) if item.dunning_level < 3 else 3
        # Das Schreiben zeigt die nächste (bzw. höchste) Mahnstufe an;
        # verbindlich wird sie erst über "Mahnstufe setzen".
        company_name = company.name

    return render_template(
        "mahnschreiben.html",
        item=item,
        company_name=company_name,
        level=level,
        level_label=DUNNING_LEVEL_LABELS[level],
        today=date.today(),
        payment_deadline=date.today() + timedelta(days=PAYMENT_DEADLINE_DAYS),
    )
