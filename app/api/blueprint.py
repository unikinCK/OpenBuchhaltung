from __future__ import annotations

from flask import Blueprint

from app.auth import require_api_token

# Blueprint-Name "api" bleibt erhalten (Endpoints wie "api.health").
api_bp = Blueprint("api", __name__, url_prefix="/api/v1")
api_bp.before_request(require_api_token)
