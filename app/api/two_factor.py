"""Zwei-Faktor-Anmeldung über die API: Status, Einrichten, Abschalten, Admin-Reset.

API-Tokens bleiben ein eigener Zugang und verlangen keinen zweiten Faktor. Einrichten und
Abschalten gibt es bewusst nicht als MCP-Tool, damit das Secret durch keinen KI-Kontext läuft.
"""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import forbidden, get_session_factory
from app.api.users import _actor, _api_can_manage_users, _managed_user_or_404, _user_dict
from app.auth import current_api_user, run_guarded_two_factor_action
from app.services.two_factor import (
    TwoFactorBlockedError,
    TwoFactorCredentialsError,
    TwoFactorError,
    confirm_setup,
    disable_two_factor,
    provisioning_uri_for,
    regenerate_recovery_codes,
    reset_two_factor,
    start_setup,
    two_factor_status,
)
from domain.models import User


def _text(payload: dict, field: str) -> str:
    value = payload.get(field)
    return value if isinstance(value, str) else ""


def _error_response(exc: TwoFactorError):
    if isinstance(exc, TwoFactorBlockedError):
        status = 429
    elif isinstance(exc, TwoFactorCredentialsError):
        status = 403
    else:
        status = 409
    return jsonify({"error": str(exc)}), status


def _no_store(payload: dict, status: int = 200):
    """Antworten mit Secret oder Codes dürfen in keinem Cache landen."""
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _own_user(session):
    api_user = current_api_user()
    if api_user is None:
        return None, (jsonify({"error": "Dafür ist ein Benutzer-Token nötig."}), 400)
    user = session.get(User, api_user["id"])
    if user is None:
        return None, (jsonify({"error": "User not found."}), 404)
    return user, None


@api_bp.get("/users/me/two-factor")
def get_two_factor_status_via_api():
    """Eigener Status; mit ``user_id`` (Administrator) der eines Benutzers."""
    raw_user_id = request.args.get("user_id", "").strip()
    target_id = None
    if raw_user_id:
        try:
            target_id = int(raw_user_id)
        except ValueError:
            return jsonify({"error": "user_id must be an integer."}), 400

    api_user = current_api_user()
    with get_session_factory()() as session:
        if target_id is None or (api_user is not None and api_user["id"] == target_id):
            user, error = _own_user(session)
        elif not _api_can_manage_users():
            return forbidden()
        else:
            user, error = _managed_user_or_404(session, target_id)
        if error is not None:
            return error
        return jsonify(two_factor_status(session, user)), 200


@api_bp.post("/users/me/two-factor/setup")
def start_two_factor_setup_via_api():
    """Beginnt die Einrichtung (aktuelles Passwort nötig) und liefert das Secret."""
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        user, error = _own_user(session)
        if error is not None:
            return error
        try:
            secret = run_guarded_two_factor_action(
                session,
                user,
                lambda: start_setup(
                    session,
                    user,
                    password=_text(payload, "current_password"),
                    actor=user.username,
                ),
            )
        except TwoFactorError as exc:
            return _error_response(exc)
        session.commit()
        return _no_store(
            {
                **two_factor_status(session, user),
                "secret": secret,
                "otpauth_uri": provisioning_uri_for(user, secret),
            },
            201,
        )


@api_bp.post("/users/me/two-factor/confirm")
def confirm_two_factor_setup_via_api():
    """Aktiviert nach gültigem Code; die Wiederherstellungscodes kommen nur in dieser Antwort."""
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        user, error = _own_user(session)
        if error is not None:
            return error
        try:
            codes = confirm_setup(
                session, user, code=_text(payload, "code"), actor=user.username
            )
        except TwoFactorError as exc:
            return _error_response(exc)
        session.commit()
        return _no_store({**two_factor_status(session, user), "recovery_codes": codes})


@api_bp.post("/users/me/two-factor/disable")
def disable_two_factor_via_api():
    """Schaltet ab: aktuelles Passwort plus Code aus der App oder Wiederherstellungscode."""
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        user, error = _own_user(session)
        if error is not None:
            return error
        try:
            run_guarded_two_factor_action(
                session,
                user,
                lambda: disable_two_factor(
                    session,
                    user,
                    password=_text(payload, "current_password"),
                    code=_text(payload, "code"),
                    actor=user.username,
                ),
            )
        except TwoFactorError as exc:
            return _error_response(exc)
        session.commit()
        return jsonify(two_factor_status(session, user)), 200


@api_bp.post("/users/me/two-factor/recovery-codes")
def regenerate_two_factor_recovery_codes_via_api():
    """Ersetzt die Wiederherstellungscodes: aktuelles Passwort plus Code."""
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        user, error = _own_user(session)
        if error is not None:
            return error
        try:
            codes = run_guarded_two_factor_action(
                session,
                user,
                lambda: regenerate_recovery_codes(
                    session,
                    user,
                    password=_text(payload, "current_password"),
                    code=_text(payload, "code"),
                    actor=user.username,
                ),
            )
        except TwoFactorError as exc:
            return _error_response(exc)
        session.commit()
        return _no_store({**two_factor_status(session, user), "recovery_codes": codes})


@api_bp.post("/users/<int:user_id>/two-factor/reset")
def reset_user_two_factor_via_api(user_id: int):
    """Administrator setzt die Zwei-Faktor-Anmeldung eines anderen Benutzers zurück."""
    if not _api_can_manage_users():
        return forbidden()
    api_user = current_api_user()
    if api_user is not None and api_user["id"] == user_id:
        return jsonify(
            {
                "error": "Die eigene Zwei-Faktor-Anmeldung lässt sich nur mit Passwort und Code "
                "abschalten (POST /users/me/two-factor/disable)."
            }
        ), 403

    with get_session_factory()() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        changed = reset_two_factor(session, user, actor=_actor())
        session.commit()
        return jsonify({**_user_dict(user), "reset": changed}), 200
