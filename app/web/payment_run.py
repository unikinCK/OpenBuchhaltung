"""SEPA-Zahllauf: pain.001-Datei aus offenen Kreditoren-Posten erzeugen."""

from __future__ import annotations

from datetime import date

from flask import Response, flash, redirect, render_template, request, url_for

from app.services.sepa_export import (
    SepaExportError,
    create_payment_run,
    payable_items_for_run,
    set_company_bank_details,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)


@main_bp.get("/zahllauf")
def payment_run_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        items = []
        company_iban = None
        company_bic = None
        if selected_company_id:
            company = require_company_access(session, selected_company_id)
            company_iban, company_bic = company.iban, company.bic
            items = payable_items_for_run(session=session, company_id=selected_company_id)

    return render_template(
        "zahllauf.html",
        companies=companies,
        selected_company_id=selected_company_id,
        items=items,
        company_iban=company_iban,
        company_bic=company_bic,
        today=date.today().isoformat(),
    )


@main_bp.post("/zahllauf/bankverbindung")
def payment_run_bank_details_action():
    company_id = request.form.get("company_id", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            set_company_bank_details(
                session=session,
                company_id=company_id,
                iban=request.form.get("iban"),
                bic=request.form.get("bic"),
                changed_by=changed_by(),
            )
        except SepaExportError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.payment_run_page", company_id=company_id))

    flash("Auftraggeber-Bankverbindung wurde gespeichert.", "success")
    return redirect(url_for("main.payment_run_page", company_id=company_id))


@main_bp.post("/zahllauf")
def payment_run_create_action():
    company_id = request.form.get("company_id", type=int)
    open_item_ids = request.form.getlist("open_item_ids", type=int)
    execution_date_raw = (request.form.get("execution_date") or "").strip()

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            execution_date = (
                date.fromisoformat(execution_date_raw) if execution_date_raw else None
            )
        except ValueError:
            flash("Ungültiges Ausführungsdatum.", "error")
            return redirect(url_for("main.payment_run_page", company_id=company_id))

        try:
            result = create_payment_run(
                session=session,
                company_id=company_id,
                open_item_ids=open_item_ids,
                execution_date=execution_date,
                changed_by=changed_by(),
            )
        except SepaExportError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.payment_run_page", company_id=company_id))

    return Response(
        result.xml_bytes,
        mimetype="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{result.file_name}"'},
    )
