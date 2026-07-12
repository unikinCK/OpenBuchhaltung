"""Benutzerverwaltung über die API."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import forbidden, get_session_factory
from app.auth import (
    ROLE_ADMIN,
    ROLE_BUCHHALTER,
    ROLE_PRUEFER,
    ROLE_SUPPORT,
    current_api_tenant_id,
    current_api_user,
    generate_api_token,
    hash_api_token,
    hash_password,
)
from domain.models import Tenant, User

ROLES = {ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER, ROLE_SUPPORT}


def _api_can_manage_users() -> bool:
    user = current_api_user()
    if user is None:
        return True
    return user["role"] == ROLE_ADMIN


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
        user = session.get(User, user_id)
        if user is None or _user_outside_api_scope(user):
            return jsonify({"error": "User not found."}), 404
        user.api_token_hash = hash_api_token(token)
        user.api_token_last4 = token[-4:]
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
        user = session.get(User, user_id)
        if user is None or _user_outside_api_scope(user):
            return jsonify({"error": "User not found."}), 404
        user.is_active = bool(payload["is_active"])
        session.commit()
        return jsonify(_user_dict(user)), 200
