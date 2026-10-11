"""Open-Banking-Zugänge (PSD2 über Enable Banking) und Umsatzabruf über die API.

Die Bank-Zustimmung erteilt der Kontoinhaber im Browser: ``…/authorize``
liefert die Bank-URL; der Rücksprung landet auf dem Web-Callback der Instanz
oder wird mit ``…/complete`` (Code + State bzw. Rücksprung-URL) abgeschlossen.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlsplit

from flask import current_app, jsonify, request, url_for

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.open_banking import (
    OpenBankingError,
    complete_open_banking_authorization,
    create_open_banking_connection,
    find_connection_by_state,
    list_open_banking_connections,
    list_open_banking_institutes,
    open_banking_config,
    serialize_connection,
    set_open_banking_connection_active,
    start_open_banking_authorization,
    sync_open_banking_transactions,
)
from domain.models import OpenBankingConnection


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _config():
    return open_banking_config(
        current_app.config,
        fallback_redirect_url=url_for("main.open_banking_callback", _external=True),
    )


def _parse_optional_date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise OpenBankingError(f"{field} muss ein ISO-Datum (YYYY-MM-DD) sein.") from exc


def _scoped_connection(session, connection_id: int) -> OpenBankingConnection | None:
    connection = session.get(OpenBankingConnection, connection_id)
    if connection is None or api_scoped_company(session, connection.company_id) is None:
        return None
    return connection


def _not_found():
    return jsonify({"error": "Open banking connection not found."}), 404


@api_bp.get("/open-banking/institutes")
def list_open_banking_institutes_via_api():
    country = request.args.get("country") or "DE"
    try:
        institutes = list_open_banking_institutes(
            config=_config(),
            country=country,
            psu_type=request.args.get("psu_type") or None,
            query=request.args.get("q") or "",
        )
    except OpenBankingError as exc:
        return jsonify({"error": str(exc)}), 422
    return jsonify({"country": country.upper(), "institutes": institutes}), 200


@api_bp.get("/open-banking-connections")
def list_open_banking_connections_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        connections = list_open_banking_connections(
            session=session, company_id=company_id, include_inactive=include_inactive
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "connections": [serialize_connection(item) for item in connections],
                }
            ),
            200,
        )


@api_bp.post("/open-banking-connections")
def create_open_banking_connection_via_api():
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
            connection = create_open_banking_connection(
                session=session,
                company_id=company.id,
                bank_account_id=int(bank_account_id),
                name=str(payload.get("name") or ""),
                aspsp_name=str(payload.get("aspsp_name") or ""),
                aspsp_country=str(payload.get("aspsp_country") or "DE"),
                psu_type=str(payload.get("psu_type") or "business"),
                iban=payload.get("iban"),
                changed_by=_api_changed_by(),
            )
        except OpenBankingError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_connection(connection)), 201


@api_bp.post("/open-banking-connections/<int:connection_id>/active")
def set_open_banking_connection_active_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    is_active = payload.get("is_active")
    if not isinstance(is_active, bool):
        return jsonify({"error": "is_active (boolean) is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        connection = _scoped_connection(session, connection_id)
        if connection is None:
            return _not_found()
        try:
            config = None if is_active else _config()
        except OpenBankingError:
            config = None  # ohne Konfiguration nur lokal deaktivieren
        try:
            connection = set_open_banking_connection_active(
                session=session,
                connection_id=connection.id,
                is_active=is_active,
                changed_by=_api_changed_by(),
                config=config,
                secret_key=current_app.config.get("SECRET_KEY"),
            )
        except OpenBankingError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_connection(connection)), 200


@api_bp.post("/open-banking-connections/<int:connection_id>/authorize")
def start_open_banking_authorization_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    valid_days = payload.get("valid_days")
    if valid_days is not None and not isinstance(valid_days, int):
        return jsonify({"error": "valid_days must be an integer."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        connection = _scoped_connection(session, connection_id)
        if connection is None:
            return _not_found()
        try:
            start = start_open_banking_authorization(
                session=session,
                connection_id=connection.id,
                config=_config(),
                valid_days=valid_days,
                changed_by=_api_changed_by(),
            )
        except OpenBankingError as exc:
            return jsonify({"error": str(exc)}), 422
        return (
            jsonify(
                {
                    "status": "authorization_pending",
                    "connection_id": start.connection_id,
                    "authorization_url": start.url,
                    "valid_until": start.valid_until.isoformat(),
                }
            ),
            200,
        )


@api_bp.post("/open-banking/complete")
def complete_open_banking_authorization_via_api():
    """Schließt eine Autorisierung mit ``code``/``state`` oder der Rücksprung-URL ab."""
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code") or "")
    state = str(payload.get("state") or "")
    redirect = str(payload.get("redirect_url") or "")
    if redirect:
        query = parse_qs(urlsplit(redirect).query)
        if query.get("error"):
            return jsonify({"error": f"Bank meldet: {query['error'][0][:200]}"}), 422
        code = code or (query.get("code") or [""])[0]
        state = state or (query.get("state") or [""])[0]
    if not code or not state:
        return jsonify({"error": "code and state (or redirect_url) are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        connection = find_connection_by_state(session, state)
        if connection is None or api_scoped_company(session, connection.company_id) is None:
            return jsonify({"error": "Authorization not found."}), 404
        try:
            connection = complete_open_banking_authorization(
                session=session,
                state=state,
                code=code,
                config=_config(),
                secret_key=current_app.config.get("SECRET_KEY"),
                changed_by=_api_changed_by(),
            )
        except OpenBankingError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_connection(connection)), 200


@api_bp.post("/open-banking-connections/<int:connection_id>/sync")
def sync_open_banking_transactions_via_api(connection_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    session_factory = get_session_factory()
    with session_factory() as session:
        connection = _scoped_connection(session, connection_id)
        if connection is None:
            return _not_found()
        try:
            report = sync_open_banking_transactions(
                session=session,
                connection_id=connection.id,
                config=_config(),
                secret_key=current_app.config.get("SECRET_KEY"),
                from_date=_parse_optional_date(payload.get("from_date"), "from_date"),
                to_date=_parse_optional_date(payload.get("to_date"), "to_date"),
                changed_by=_api_changed_by(),
            )
        except OpenBankingError as exc:
            return jsonify({"error": str(exc)}), 422
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
