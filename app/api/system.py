"""System-Endpunkte (Health-Check, ohne Auth)."""

from __future__ import annotations

from flask import current_app, jsonify

from app.api.blueprint import api_bp
from app.api.helpers import get_session_factory
from app.db import database_health


@api_bp.get("/health")
def health():
    """Liveness/Readiness: ``SELECT 1``, Alembic-Revision gegen Head, Version und Commit.

    200 mit ``status: ok``, wenn die Datenbank antwortet und das Schema auf dem
    Head steht; sonst 503 mit ``status: unhealthy`` (Docker-HEALTHCHECK und
    Monitoring werten den Statuscode aus).
    """
    checks = database_health(get_session_factory())
    healthy = checks["database"]["ok"] and checks["schema"]["up_to_date"] is not False
    payload = {
        "status": "ok" if healthy else "unhealthy",
        "version": current_app.config.get("APP_VERSION"),
        "commit": current_app.config.get("APP_COMMIT_SHA"),
        **checks,
    }
    return jsonify(payload), (200 if healthy else 503)
