"""UStVA: Kennziffern-Vorschau je Zeitraum und Festhalten von Voranmeldungen."""

from __future__ import annotations

from datetime import date

from flask import current_app, flash, redirect, render_template, request, url_for

from app.services.elster import (
    ElsterError,
    elster_readiness,
    list_elster_submissions,
    submit_vat_return,
)
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return,
    list_vat_returns,
    period_bounds,
    save_vat_return,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)


def _selected_period_label() -> str:
    """Zeitraum aus den Query-Parametern; Default: Vormonat."""
    raw = request.args.get("period", "").strip()
    if raw:
        return raw
    today = date.today()
    if today.month == 1:
        return f"{today.year - 1}-12"
    return f"{today.year}-{today.month - 1:02d}"


@main_bp.get("/ustva")
def vat_returns_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        rows = []
        saved_returns = []
        period_label = _selected_period_label()
        period_error = None
        date_from = date_to = None
        if selected_company_id:
            try:
                date_from, date_to, period_label = period_bounds(period_label)
                rows = compute_vat_return(
                    session=session,
                    company_id=selected_company_id,
                    date_from=date_from,
                    date_to=date_to,
                )
            except VatReturnError as exc:
                period_error = str(exc)
            saved_returns = list_vat_returns(session=session, company_id=selected_company_id)
            submissions = list_elster_submissions(
                session=session, company_id=selected_company_id
            )
            latest_submission_by_return = {}
            for submission in submissions:
                latest_submission_by_return.setdefault(submission.vat_return_id, submission)
            # Kennzahlen der Snapshots für die Anzeige vorab laden.
            saved_views = [
                {
                    "id": item.id,
                    "period_label": item.period_label,
                    "date_from": item.date_from,
                    "date_to": item.date_to,
                    "status": item.status,
                    "created_at": item.created_at,
                    "created_by": item.created_by,
                    "kennzahlen": item.kennzahlen,
                    "latest_elster_submission": latest_submission_by_return.get(item.id),
                }
                for item in saved_returns
            ]
        else:
            saved_views = []

    return render_template(
        "ustva.html",
        companies=companies,
        selected_company_id=selected_company_id,
        period_label=period_label,
        period_error=period_error,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        saved_returns=saved_views,
        current_year=date.today().year,
        elster_readiness=elster_readiness(current_app.config),
    )


@main_bp.post("/ustva/festhalten")
def save_vat_return_action():
    company_id = request.form.get("company_id", type=int)
    period_label = request.form.get("period", "").strip()

    if not company_id or not period_label:
        flash("Gesellschaft und Zeitraum sind Pflichtfelder.", "error")
        return redirect(url_for("main.vat_returns_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            vat_return = save_vat_return(
                session=session,
                company_id=company_id,
                period_label=period_label,
                changed_by=changed_by(),
            )
        except VatReturnError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("main.vat_returns_page", company_id=company_id, period=period_label)
            )

    flash(f"UStVA {vat_return.period_label} wurde festgehalten.", "success")
    return redirect(
        url_for("main.vat_returns_page", company_id=company_id, period=vat_return.period_label)
    )


@main_bp.post("/ustva/<int:vat_return_id>/elster-test")
def submit_vat_return_elster_test_action(vat_return_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        from domain.models import VatReturn

        vat_return = session.get(VatReturn, vat_return_id)
        if vat_return is None:
            flash("UStVA wurde nicht gefunden.", "error")
            return redirect(url_for("main.vat_returns_page"))
        require_company_access(session, vat_return.company_id)
        company_id = vat_return.company_id
        period_label = vat_return.period_label
        try:
            submission = submit_vat_return(
                session=session,
                vat_return_id=vat_return.id,
                environment="test",
                transport="mock",
                changed_by=changed_by(),
            )
        except ElsterError as exc:
            flash(f"ELSTER-Testübermittlung fehlgeschlagen: {exc}", "error")
            return redirect(
                url_for("main.vat_returns_page", company_id=company_id, period=period_label)
            )

    flash(f"ELSTER-Testübermittlung protokolliert: {submission.transfer_ticket}", "success")
    return redirect(url_for("main.vat_returns_page", company_id=company_id, period=period_label))
