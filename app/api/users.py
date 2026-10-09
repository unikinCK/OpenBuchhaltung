"""Benutzerverwaltung über die API."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

from app.api.blueprint import api_bp
from app.api.helpers import forbidden, get_session_factory
from app.auth import (
    ROLE_ADMIN,
    ROLE_BUCHHALTER,
    ROLE_PRUEFER,
    ROLE_SUPPORT,
    api_has_global_access,
    current_api_tenant_id,
    current_api_user,
    generate_api_token,
    hash_api_token,
    hash_password,
    password_policy_error,
    unlock_user_login,
)
from app.services.llm_settings import (
    LlmSettingsError,
    apply_user_llm_settings,
    clear_user_llm_settings,
    llm_settings_audit_payload,
    user_llm_summary,
)
from app.services.security_events import record_security_event
from domain.models import Tenant, User

ROLES = {ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER, ROLE_SUPPORT}


def _api_can_manage_users() -> bool:
    user = current_api_user()
    if user is None:
        return True
    return user["role"] == ROLE_ADMIN


def _actor() -> str:
    user = current_api_user()
    return user["username"] if user else "api-token"


def _visible_user_filter(stmt):
    tenant_id = current_api_tenant_id()
    if tenant_id is None:
        return stmt
    return stmt.where(User.tenant_id == tenant_id)


def _user_dict(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "is_active": user.is_active,
        "api_token_last4": user.api_token_last4,
        "llm": user_llm_summary(user),
        "created_at": user.created_at.isoformat(),
    }


def _user_outside_api_scope(user: User) -> bool:
    tenant_id = current_api_tenant_id()
    return tenant_id is not None and user.tenant_id != tenant_id


def _parse_tenant_id(raw_tenant_id) -> int | None:
    if raw_tenant_id in {None, ""}:
        return None
    return int(raw_tenant_id)


def _validate_target_tenant(session, tenant_id: int | None):
    current_tenant_id = current_api_tenant_id()
    if current_tenant_id is not None and tenant_id != current_tenant_id:
        return None, jsonify({"error": "Tenant is outside your scope."}), 403
    if tenant_id is None:
        if current_tenant_id is not None:
            return None, jsonify({"error": "Tenant-bound admins must set their tenant."}), 403
        return None, None, None
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return None, jsonify({"error": "Tenant not found."}), 404
    return tenant, None, None


def _managed_user_or_404(session, user_id: int):
    user = session.get(User, user_id)
    if user is None or _user_outside_api_scope(user):
        return None, (jsonify({"error": "User not found."}), 404)
    return user, None


@api_bp.get("/users")
def list_users_via_api():
    if not _api_can_manage_users():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        stmt = _visible_user_filter(select(User).order_by(User.username))
        users = session.execute(stmt).scalars().all()
        return jsonify({"users": [_user_dict(user) for user in users]}), 200


@api_bp.post("/users")
def create_user_via_api():
    if not _api_can_manage_users():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    role = (payload.get("role") or ROLE_BUCHHALTER).strip()
    try:
        tenant_id = _parse_tenant_id(payload.get("tenant_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "tenant_id must be an integer or null."}), 400

    if not username or not password:
        return jsonify({"error": "username and password are required."}), 400
    if role not in ROLES:
        return jsonify({"error": "role must be Admin, Buchhalter, Pruefer or Support."}), 400
    policy_error = password_policy_error(password)
    if policy_error:
        return jsonify({"error": policy_error}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        _, error_response, status_code = _validate_target_tenant(session, tenant_id)
        if error_response is not None:
            return error_response, status_code

        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id,
            is_active=bool(payload.get("is_active", True)),
        )
        session.add(user)
        try:
            session.flush()
            record_security_event(
                session,
                user=user,
                action="created",
                actor=_actor(),
                payload={"username": username, "role": role, "tenant_id": tenant_id},
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "User already exists."}), 409
        return jsonify(_user_dict(user)), 201


@api_bp.post("/users/<int:user_id>/api-token")
def rotate_user_api_token_via_api(user_id: int):
    if not _api_can_manage_users():
        return forbidden()

    session_factory = get_session_factory()
    token = generate_api_token()
    with session_factory() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        user.api_token_hash = hash_api_token(token)
        user.api_token_last4 = token[-4:]
        record_security_event(
            session,
            user=user,
            action="api_token_rotated",
            actor=_actor(),
            payload={"api_token_last4": token[-4:]},
        )
        session.commit()
        payload = _user_dict(user)
        payload["api_token"] = token
        return jsonify(payload), 201


@api_bp.post("/users/<int:user_id>/active")
def set_user_active_via_api(user_id: int):
    if not _api_can_manage_users():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    if "is_active" not in payload:
        return jsonify({"error": "is_active is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        is_active = bool(payload["is_active"])
        user.is_active = is_active
        record_security_event(
            session,
            user=user,
            action="activated" if is_active else "deactivated",
            actor=_actor(),
        )
        session.commit()
        return jsonify(_user_dict(user)), 200


@api_bp.post("/users/<int:user_id>/llm")
def set_user_llm_settings_via_api(user_id: int):
    """Administrator hinterlegt den KI-Zugang (LLM-API-Key) eines Benutzers.

    ``provider`` ist ``openai`` (Standard) oder ``custom`` mit ``endpoint_url``;
    ohne ``api_key`` bleibt ein bereits gespeicherter Key erhalten.
    """
    if not _api_can_manage_users():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    for field in ("provider", "endpoint_url", "model", "api_key"):
        if payload.get(field) is not None and not isinstance(payload[field], str):
            return jsonify({"error": f"{field} must be a string."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        try:
            apply_user_llm_settings(
                user,
                provider=payload.get("provider"),
                endpoint_url=payload.get("endpoint_url"),
                model=payload.get("model"),
                api_key=payload.get("api_key"),
            )
        except LlmSettingsError as exc:
            return jsonify({"error": str(exc)}), 400
        record_security_event(
            session,
            user=user,
            action="llm_settings_updated",
            actor=_actor(),
            payload={
                **llm_settings_audit_payload(user),
                "api_key_changed": bool((payload.get("api_key") or "").strip()),
            },
        )
        session.commit()
        return jsonify(_user_dict(user)), 200


@api_bp.post("/users/<int:user_id>/llm/delete")
def clear_user_llm_settings_via_api(user_id: int):
    """Entfernt den KI-Zugang eines Benutzers (danach gelten die Instanz-Endpoints)."""
    if not _api_can_manage_users():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        clear_user_llm_settings(user)
        record_security_event(session, user=user, action="llm_settings_cleared", actor=_actor())
        session.commit()
        return jsonify(_user_dict(user)), 200


@api_bp.post("/users/<int:user_id>/unlock")
def unlock_user_login_via_api(user_id: int):
    """Hebt die Login-Sperre (Rate-Limit nach Fehlversuchen) eines Benutzers auf."""
    if not _api_can_manage_users():
        return forbidden()

    session_factory = get_session_factory()
    with session_factory() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        removed = unlock_user_login(session, username=user.username)
        record_security_event(
            session,
            user=user,
            action="login_unlocked",
            actor=_actor(),
            payload={"removed_attempts": removed},
        )
        session.commit()
        payload = _user_dict(user)
        payload["removed_attempts"] = removed
        return jsonify(payload), 200


@api_bp.post("/users/<int:user_id>/password")
def set_user_password_via_api(user_id: int):
    """Administrator setzt das Passwort eines Benutzers neu."""
    if not _api_can_manage_users():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    new_password = payload.get("new_password") or ""
    policy_error = password_policy_error(new_password)
    if policy_error:
        return jsonify({"error": policy_error}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        user, error = _managed_user_or_404(session, user_id)
        if error is not None:
            return error
        user.password_hash = hash_password(new_password)
        record_security_event(session, user=user, action="password_reset", actor=_actor())
        session.commit()
        return jsonify(_user_dict(user)), 200


@api_bp.get("/users/me")
def get_current_api_identity():
    """Identität des Aufrufers: Benutzer (Token oder Session) bzw. globaler API-Token."""
    api_user = current_api_user()
    if api_user is not None:
        auth = "user"
    elif api_has_global_access():
        auth = "api_token"
    else:
        auth = "anonymous"
    return jsonify(
        {
            "auth": auth,
            "user": dict(api_user) if api_user is not None else None,
            "global_access": api_has_global_access(),
        }
    ), 200


@api_bp.post("/users/me/password")
def change_own_password_via_api():
    """Eigenes Passwort ändern (Benutzer-Token; aktuelles Passwort erforderlich)."""
    api_user = current_api_user()
    if api_user is None:
        return jsonify({"error": "A user token is required to change a password."}), 400

    payload = request.get_json(silent=True) or {}
    current_password = payload.get("current_password") or ""
    new_password = payload.get("new_password") or ""
    policy_error = password_policy_error(new_password)
    if policy_error:
        return jsonify({"error": policy_error}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        user = session.get(User, api_user["id"])
        if user is None or not check_password_hash(user.password_hash, current_password):
            record_security_event(
                session,
                user=user,
                action="password_change_failed",
                actor=api_user["username"],
                audit=False,
            )
            return jsonify({"error": "Current password is incorrect."}), 403
        user.password_hash = hash_password(new_password)
        record_security_event(session, user=user, action="password_changed", actor=user.username)
        session.commit()
        return jsonify(_user_dict(user)), 200
