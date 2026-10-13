"""Push-Mitteilungen: Status, Geräteregistrierung, Ereignisauswahl, Test und Widerruf."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import get_session_factory
from app.auth import ROLE_ADMIN, current_api_user
from app.services.push import (
    PushError,
    device_dict,
    enqueue_test_notification,
    list_devices,
    push_status,
    register_device,
    revoke_device,
    update_device,
)
from domain.models import PushDevice, User


def _sees_all_devices() -> bool:
    """Globaler API-Token oder globaler Admin sieht/verwaltet alle Geräte."""
    user = current_api_user()
    if user is None:
        return True
    return user["role"] == ROLE_ADMIN and user.get("tenant_id") is None


def _actor() -> str:
    user = current_api_user()
    return user["username"] if user else "api-token"


def _visible_device(session, device_id: int) -> PushDevice | None:
    device = session.get(PushDevice, device_id)
    user = current_api_user()
    if device is None or (not _sees_all_devices() and device.user_id != user["id"]):
        return None
    return device


@api_bp.get("/push/status")
def get_push_status():
    return jsonify(push_status()), 200


@api_bp.get("/devices")
def list_push_devices():
    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}
    user = current_api_user()
    with get_session_factory()() as session:
        devices = list_devices(
            session,
            user_id=None if _sees_all_devices() else user["id"],
            include_inactive=include_inactive,
        )
    return jsonify({"devices": devices}), 200


@api_bp.post("/devices")
def register_push_device():
    api_user = current_api_user()
    if api_user is None:
        return (
            jsonify({"error": "Geräte können nur mit einem Benutzer-Token registriert werden."}),
            400,
        )
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        user = session.get(User, api_user["id"])
        try:
            device = register_device(
                session,
                user=user,
                device_token=payload.get("device_token"),
                environment=payload.get("environment"),
                platform=payload.get("platform"),
                device_name=payload.get("device_name"),
                app_version=payload.get("app_version"),
                server_url=payload.get("server_url") or request.url_root.rstrip("/"),
                events=payload.get("events"),
            )
        except PushError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 400
        session.commit()
        return jsonify(device_dict(device)), 201


@api_bp.patch("/devices/<int:device_id>")
def update_push_device(device_id: int):
    payload = request.get_json(silent=True) or {}
    with get_session_factory()() as session:
        device = _visible_device(session, device_id)
        if device is None:
            return jsonify({"error": "Device not found."}), 404
        if device.revoked_at is not None:
            return jsonify({"error": "Das Gerät ist widerrufen."}), 409
        try:
            update_device(
                session,
                device,
                actor=_actor(),
                events=payload.get("events"),
                device_name=payload.get("device_name"),
            )
        except PushError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 400
        session.commit()
        return jsonify(device_dict(device)), 200


@api_bp.post("/devices/<int:device_id>/revoke")
def revoke_push_device(device_id: int):
    with get_session_factory()() as session:
        device = _visible_device(session, device_id)
        if device is None:
            return jsonify({"error": "Device not found."}), 404
        if device.revoked_at is None:
            revoke_device(session, device, actor=_actor())
            session.commit()
        return jsonify(device_dict(device)), 200


@api_bp.post("/devices/<int:device_id>/test")
def send_push_test(device_id: int):
    with get_session_factory()() as session:
        device = _visible_device(session, device_id)
        if device is None:
            return jsonify({"error": "Device not found."}), 404
        try:
            notification = enqueue_test_notification(session, device)
        except PushError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 409
        session.commit()
        return jsonify({"notification_id": notification.id, "status": "pending"}), 202
