"""UStVA: Kennziffern-Vorschau je Zeitraum und Festhalten von Voranmeldungen."""

from __future__ import annotations

from datetime import date

from flask import (
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from app.services.elster import (
    ElsterError,
    elster_payload_filename,
    elster_payload_hash_matches,
    elster_readiness,
    elster_submission_summary,
    get_elster_submission,
    list_elster_submissions,
    preflight_vat_return,
    retry_elster_submission,
    submit_vat_return,
)
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return,
    list_vat_returns,
    period_bounds,
    save_vat_return,
    vat_return_display_name,
    vat_return_kind_from_label,
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


def _selected_elster_filters() -> dict[str, str | None]:
    return {
        "status": (request.args.get("elster_status") or "").strip() or None,
        "transport": (request.args.get("elster_transport") or "").strip() or None,
        "environment": (request.args.get("elster_environment") or "").strip() or None,
    }


@main_bp.get("/ustva")
def vat_returns_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        rows = []
        saved_returns = []
        period_label = _selected_period_label()
        period_error = None
        elster_filter_error = None
        elster_filters = _selected_elster_filters()
        elster_summary = None
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
            elster_summary = elster_submission_summary(
                session=session, company_id=selected_company_id
            )
            try:
                submissions = list_elster_submissions(
                    session=session,
                    company_id=selected_company_id,
                    status=elster_filters["status"],
                    transport=elster_filters["transport"],
                    environment=elster_filters["environment"],
                )
            except ElsterError as exc:
                elster_filter_error = str(exc)
                submissions = []
            submissions_by_return = {}
            for submission in submissions:
                submission.payload_hash_valid = elster_payload_hash_matches(submission)
                submissions_by_return.setdefault(submission.vat_return_id, []).append(
                    submission
                )
            # Kennzahlen der Snapshots für die Anzeige vorab laden.
            saved_views = [
                {
                    "id": item.id,
                    "period_label": item.period_label,
                    "declaration_display_name": vat_return_display_name(
                        item.period_label
                    ),
                    "declaration_type": vat_return_kind_from_label(item.period_label),
                    "date_from": item.date_from,
                    "date_to": item.date_to,
                    "status": item.status,
                    "created_at": item.created_at,
                    "created_by": item.created_by,
                    "kennzahlen": item.kennzahlen,
                    "elster_submissions": submissions_by_return.get(item.id, []),
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
        declaration_display_name=vat_return_display_name(period_label),
        period_error=period_error,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        saved_returns=saved_views,
        current_year=date.today().year,
        elster_readiness=elster_readiness(current_app.config),
        elster_summary=elster_summary,
        elster_filters=elster_filters,
        elster_filter_error=elster_filter_error,
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

    display_name = vat_return_display_name(vat_return.period_label)
    flash(f"{display_name} {vat_return.period_label} wurde festgehalten.", "success")
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


@main_bp.post("/ustva/<int:vat_return_id>/elster-preflight")
def preflight_vat_return_elster_action(vat_return_id: int):
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
        result = preflight_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="test",
            transport="mock",
            config=current_app.config,
        )

    if result["ok"]:
        flash(
            "ELSTER-Preflight ok: "
            f"{result['period_label']}, Hash {str(result['payload_hash'])[:12]}",
            "success",
        )
    else:
        flash(f"ELSTER-Preflight fehlgeschlagen: {', '.join(result['errors'])}", "error")
    return redirect(url_for("main.vat_returns_page", company_id=company_id, period=period_label))


@main_bp.post("/ustva/elster-submissions/<int:submission_id>/retry")
def retry_elster_submission_action(submission_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        submission = get_elster_submission(
            session=session, submission_id=submission_id
        )
        if submission is None:
            abort(404)
        require_company_access(session, submission.company_id)
        company_id = submission.company_id
        period_label = submission.vat_return.period_label
        if submission.status != "failed":
            flash(
                "Nur fehlgeschlagene ELSTER-Übermittlungen können erneut versucht werden.",
                "error",
            )
            return redirect(
                url_for("main.vat_returns_page", company_id=company_id, period=period_label)
            )
        try:
            retry = retry_elster_submission(
                session=session,
                submission_id=submission.id,
                changed_by=changed_by(),
                config=current_app.config,
            )
        except ElsterError as exc:
            flash(f"ELSTER-Retry fehlgeschlagen: {exc}", "error")
            return redirect(
                url_for("main.vat_returns_page", company_id=company_id, period=period_label)
            )

    flash(f"ELSTER-Retry protokolliert: {retry.transfer_ticket}", "success")
    return redirect(url_for("main.vat_returns_page", company_id=company_id, period=period_label))


@main_bp.get("/ustva/elster-submissions/<int:submission_id>/payload.xml")
def download_elster_payload_action(submission_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        submission = get_elster_submission(
            session=session, submission_id=submission_id
        )
        if submission is None:
            abort(404)
        require_company_access(session, submission.company_id)
        response = make_response(submission.payload_xml)
        response.headers["Content-Type"] = "application/xml; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{elster_payload_filename(submission)}"'
        )
        return response
