"""Per OAuth verbundene Apps (MCP-Connectoren) anzeigen und widerrufen."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import get_session_factory
from app.auth import ROLE_ADMIN, current_api_user
from app.oauth import grant_dict, list_grants, revoke_grant
from domain.models import OAuthGrant


def _sees_all_grants() -> bool:
    """Globaler API-Token oder globaler Admin sieht/verwaltet alle Grants."""
    user = current_api_user()
    if user is None:
        return True
    return user["role"] == ROLE_ADMIN and user.get("tenant_id") is None


def _actor() -> str:
    user = current_api_user()
    return user["username"] if user else "api-token"


@api_bp.get("/oauth/grants")
def list_oauth_grants():
    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}
    user = current_api_user()
    with get_session_factory()() as session:
        grants = list_grants(
            session,
            user_id=None if _sees_all_grants() else user["id"],
            include_inactive=include_inactive,
        )
    return jsonify(grants), 200


@api_bp.post("/oauth/grants/<int:grant_id>/revoke")
def revoke_oauth_grant_via_api(grant_id: int):
    user = current_api_user()
    with get_session_factory()() as session:
        grant = session.get(OAuthGrant, grant_id)
        if grant is None or (not _sees_all_grants() and grant.user_id != user["id"]):
            return jsonify({"error": "OAuth grant not found."}), 404
        if grant.revoked_at is None:
            revoke_grant(session, grant, actor=_actor())
            session.commit()
        return jsonify(grant_dict(grant)), 200
