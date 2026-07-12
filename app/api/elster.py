"""ELSTER-Übermittlungen über die API."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.elster import ElsterError, list_elster_submissions, submit_vat_return
from domain.models import ElsterSubmission, VatReturn


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _submission_dict(submission: ElsterSubmission) -> dict[str, object]:
    return {
        "id": submission.id,
        "tenant_id": submission.tenant_id,
        "company_id": submission.company_id,
        "vat_return_id": submission.vat_return_id,
        "procedure": submission.procedure,
        "environment": submission.environment,
        "status": submission.status,
        "transport": submission.transport,
        "certificate_alias": submission.certificate_alias,
        "payload_hash": submission.payload_hash,
        "response_protocol": submission.response_protocol,
        "transfer_ticket": submission.transfer_ticket,
        "created_at": submission.created_at.isoformat(),
        "submitted_at": submission.submitted_at.isoformat()
        if submission.submitted_at is not None
        else None,
        "created_by": submission.created_by,
    }


@api_bp.get("/elster/submissions")
def list_elster_submissions_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    vat_return_id = request.args.get("vat_return_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        submissions = list_elster_submissions(
            session=session, company_id=company_id, vat_return_id=vat_return_id
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "submissions": [_submission_dict(item) for item in submissions],
                }
            ),
            200,
        )


@api_bp.post("/elster/ustva/submit")
def submit_vat_return_elster_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        vat_return_id = int(payload.get("vat_return_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "vat_return_id is required."}), 400

    environment = (payload.get("environment") or "test").strip()
    transport = (payload.get("transport") or "mock").strip()
    certificate_alias = (payload.get("certificate_alias") or "").strip() or None

    session_factory = get_session_factory()
    with session_factory() as session:
        vat_return = session.get(VatReturn, vat_return_id)
        if vat_return is None or api_scoped_company(session, vat_return.company_id) is None:
            return jsonify({"error": "UStVA not found."}), 404
        try:
            submission = submit_vat_return(
                session=session,
                vat_return_id=vat_return.id,
                environment=environment,
                transport=transport,
                certificate_alias=certificate_alias,
                changed_by=_api_changed_by(),
            )
        except ElsterError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_submission_dict(submission)), 201
