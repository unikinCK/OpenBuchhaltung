"""Gemeinsame Helfer für alle API-Routenmodule."""

from __future__ import annotations

from datetime import date

from flask import current_app, jsonify, request

from app.auth import (
    ROLE_ADMIN,
    WRITE_ROLES,
    current_api_tenant_id,
    current_api_user,
)
from domain.models import Company


def validation_error(message: str, *, details: list[dict[str, str]] | None = None):
    payload: dict[str, object] = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), 422


def incoming_invoice_error_response(exc):
    """Mappt IncomingInvoiceError-Codes auf die API-Fehlerkonvention."""
    if exc.code == "account_not_found":
        return jsonify({"error": "Account not found."}), 404
    if exc.code == "tax_code_not_found":
        return jsonify({"error": "Tax code not found."}), 404
    return jsonify({"error": str(exc)}), 422


def get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


def api_scoped_company(session, company_id: int) -> Company | None:
    company = session.get(Company, company_id)
    if company is None:
        return None
    tenant_id = current_api_tenant_id()
    if tenant_id is not None and company.tenant_id != tenant_id:
        return None
    return company


def api_can_write() -> bool:
    user = current_api_user()
    if user is None:
        return True
    return user["role"] in WRITE_ROLES


def api_can_create_tenant() -> bool:
    user = current_api_user()
    if user is None:
        return True
    return user["role"] == ROLE_ADMIN and user.get("tenant_id") is None


def forbidden():
    return jsonify({"error": "Forbidden."}), 403


TRUE_VALUES = {"1", "true", "yes", "ja", "on"}


def bool_arg(name: str, *, default: bool = False) -> bool:
    """Liest einen booleschen Query-Parameter (1/true/yes/ja/on, Groß-/Kleinschreibung egal)."""
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in TRUE_VALUES


def bool_payload(value: object, *, default: bool = False) -> bool:
    """Interpretiert einen JSON-Wert (bool, Zahl oder String) als Schalter."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in TRUE_VALUES


class DateArgError(ValueError):
    """Raised when a date query parameter is not ISO-formatted."""


def date_arg(name: str) -> date | None:
    """Liest einen optionalen Datums-Query-Parameter (Format JJJJ-MM-TT)."""
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise DateArgError(
            f"{name} must be an ISO date (YYYY-MM-DD), got {raw!r}."
        ) from exc
