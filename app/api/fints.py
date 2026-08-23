"""FinTS-Bankzugänge und Umsatzabruf über die API.

PIN und TAN werden nur durchgereicht, nie gespeichert oder geloggt.
"""

from __future__ import annotations

from datetime import date

from flask import current_app, jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.fints_sync import (
    FinTSSyncError,
    FinTSSyncResult,
    create_fints_connection,
    list_fints_connections,
    serialize_connection,
    set_fints_connection_active,
    start_fints_sync,
    submit_fints_tan,
)
from domain.models import FinTSConnection, FinTSPendingDialog


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _parse_optional_date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FinTSSyncError(f"{field} muss ein ISO-Datum (YYYY-MM-DD) sein.") from exc


def _sync_result_response(result: FinTSSyncResult):
    if result.challenge is not None:
        return (
            jsonify(
                {
                    "status": "tan_required",
                    "dialog_id": result.challenge.dialog_id,
                    "challenge": result.challenge.challenge,
                    "decoupled": result.challenge.decoupled,
                }
            ),
            202,
        )
    report = result.report
    return (
        jsonify(
            {
                "status": "imported",
                "report": {
                    "total_rows": report.total_rows,
                    "imported_rows": report.imported_rows,
                    "duplicate_rows": report.duplicate_rows,
                    "error_rows": report.error_rows,
                },
            }
        ),
        200,
    )


@api_bp.get("/fints-connections")
def list_fints_connections_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        connections = list_fints_connections(
            session=session, company_id=company_id, include_inactive=include_inactive
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "connections": [
                        serialize_connection(connection) for connection in connections
                    ],
                }
            ),
            200,
        )


@api_bp.post("/fints-connections")
def create_fints_connection_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    bank_account_id = payload.get("bank_account_id")
    if not company_id or not bank_account_id:
        return jsonify({"error": "company_id and bank_account_id are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            connection = create_fints_connection(
                session=session,
                company_id=company.id,
                bank_account_id=int(bank_account_id),
                name=str(payload.get("name") or ""),
                blz=str(payload.get("blz") or ""),
                login=str(payload.get("login") or ""),
                fints_url=str(payload.get("fints_url") or ""),
                sepa_iban=payload.get("sepa_iban"),
                changed_by=_api_changed_by(),
            )
        except FinTSSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_connection(connection)), 201


@api_bp.post("/fints-connections/<int:connection_id>/active")
def set_fints_connection_active_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    is_active = payload.get("is_active")
    if not isinstance(is_active, bool):
        return jsonify({"error": "is_active (boolean) is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        connection = session.get(FinTSConnection, connection_id)
        if connection is None or api_scoped_company(session, connection.company_id) is None:
            return jsonify({"error": "FinTS connection not found."}), 404
        try:
            connection = set_fints_connection_active(
                session=session,
                connection_id=connection.id,
                is_active=is_active,
                changed_by=_api_changed_by(),
            )
        except FinTSSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_connection(connection)), 200


@api_bp.post("/fints-connections/<int:connection_id>/sync")
def sync_fints_transactions_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    pin = str(payload.get("pin") or "")

    session_factory = get_session_factory()
    with session_factory() as session:
        connection = session.get(FinTSConnection, connection_id)
        if connection is None or api_scoped_company(session, connection.company_id) is None:
            return jsonify({"error": "FinTS connection not found."}), 404
        try:
            result = start_fints_sync(
                session=session,
                connection_id=connection.id,
                pin=pin,
                product_id=current_app.config.get("FINTS_PRODUCT_ID"),
                from_date=_parse_optional_date(payload.get("from_date"), "from_date"),
                to_date=_parse_optional_date(payload.get("to_date"), "to_date"),
                changed_by=_api_changed_by(),
            )
        except FinTSSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return _sync_result_response(result)


@api_bp.post("/fints-dialogs/<dialog_id>/tan")
def submit_fints_tan_via_api(dialog_id: str):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    pin = str(payload.get("pin") or "")
    tan = payload.get("tan")

    session_factory = get_session_factory()
    with session_factory() as session:
        pending = session.get(FinTSPendingDialog, dialog_id)
        if pending is None or api_scoped_company(session, pending.company_id) is None:
            return jsonify({"error": "FinTS dialog not found."}), 404
        try:
            result = submit_fints_tan(
                session=session,
                dialog_id=dialog_id,
                pin=pin,
                tan=str(tan) if tan is not None else None,
                product_id=current_app.config.get("FINTS_PRODUCT_ID"),
                changed_by=_api_changed_by(),
            )
        except FinTSSyncError as exc:
            return jsonify({"error": str(exc)}), 422
        return _sync_result_response(result)
