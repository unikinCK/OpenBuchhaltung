from __future__ import annotations

from flask import Blueprint

from app.auth import require_ui_login

# Blueprint-Name "main" bleibt erhalten, damit alle url_for("main.…")-Referenzen
# in Templates und Tests unverändert funktionieren.
main_bp = Blueprint("main", __name__)
main_bp.before_request(require_ui_login)
