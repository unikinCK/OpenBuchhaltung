"""ELSTER-Übermittlungen über die API."""

from __future__ import annotations

from flask import current_app, jsonify, make_response, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.elster import (
    ElsterError,
    elster_payload_filename,
    elster_payload_hash_matches,
    elster_readiness,
    get_elster_submission,
    list_elster_submissions,
    submit_vat_return,
)
from domain.models import ElsterSubmission, VatReturn


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _submission_dict(
    submission: ElsterSubmission, *, include_payload: bool = False
) -> dict[str, object]:
    payload = {
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
        "payload_hash_valid": elster_payload_hash_matches(submission),
        "response_protocol": submission.response_protocol,
        "transfer_ticket": submission.transfer_ticket,
        "created_at": submission.created_at.isoformat(),
        "submitted_at": submission.submitted_at.isoformat()
        if submission.submitted_at is not None
        else None,
        "created_by": submission.created_by,
    }
    if include_payload:
        payload["payload_xml"] = submission.payload_xml
    return payload


@api_bp.get("/elster/readiness")
def get_elster_readiness_via_api():
    return jsonify(elster_readiness(current_app.config)), 200


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


@api_bp.get("/elster/submissions/<int:submission_id>")
def get_elster_submission_via_api(submission_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        submission = get_elster_submission(
            session=session, submission_id=submission_id
        )
        if (
            submission is None
            or api_scoped_company(session, submission.company_id) is None
        ):
            return jsonify({"error": "ELSTER submission not found."}), 404
        return jsonify(_submission_dict(submission, include_payload=True)), 200


@api_bp.get("/elster/submissions/<int:submission_id>/payload.xml")
def download_elster_payload_via_api(submission_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        submission = get_elster_submission(
            session=session, submission_id=submission_id
        )
        if (
            submission is None
            or api_scoped_company(session, submission.company_id) is None
        ):
            return jsonify({"error": "ELSTER submission not found."}), 404
        response = make_response(submission.payload_xml)
        response.headers["Content-Type"] = "application/xml; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{elster_payload_filename(submission)}"'
        )
        return response


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
                config=current_app.config,
            )
        except ElsterError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(_submission_dict(submission)), 201
