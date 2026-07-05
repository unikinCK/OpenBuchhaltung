from __future__ import annotations

import secrets
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from domain.models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.before_request
def _protect_auth_posts():
    validate_csrf()

ROLE_ADMIN = "Admin"
ROLE_BUCHHALTER = "Buchhalter"
ROLE_PRUEFER = "Pruefer"
WRITE_ROLES = {ROLE_ADMIN, ROLE_BUCHHALTER}


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def current_user() -> dict | None:
    return session.get("user")


def current_tenant_id() -> int | None:
    """Tenant scope of the logged-in user; None means global access (Admin ohne Tenant)."""
    user = current_user()
    if user is None:
        return None
    return user.get("tenant_id")


def ensure_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["_csrf_token"] = token
    return token


def validate_csrf() -> None:
    """Bricht schreibende Requests ohne gültigen CSRF-Token mit 400 ab."""
    if not current_app.config.get("CSRF_PROTECT", True):
        return
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    token = session.get("_csrf_token", "")
    submitted = request.form.get("_csrf_token", "")
    if not token or not submitted or not secrets.compare_digest(token, submitted):
        abort(400, description="CSRF-Token fehlt oder ist ungültig.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Bitte zuerst anmelden.", "error")
            return redirect(url_for("auth.login_form", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def require_ui_login():
    """before_request-Hook: UI-Routen erfordern Anmeldung, Schreibaktionen eine Schreibrolle."""
    user = current_user()
    if user is None:
        flash("Bitte zuerst anmelden.", "error")
        return redirect(url_for("auth.login_form", next=request.path))

    if request.method not in {"GET", "HEAD", "OPTIONS"} and user["role"] not in WRITE_ROLES:
        flash("Ihre Rolle erlaubt nur Lesezugriff.", "error")
        return redirect(url_for("main.index"))

    validate_csrf()
    return None


def require_api_token():
    """before_request-Hook für die API: prüft Bearer-Token, falls API_AUTH_TOKEN gesetzt ist.

    Ohne konfigurierten Token bleibt die API offen (Entwicklungsmodus);
    vollwertige API-Tokens je Benutzer folgen in Phase 3.
    """
    configured_token = current_app.config.get("API_AUTH_TOKEN")
    if not configured_token:
        return None

    if request.endpoint == "api.health":
        return None

    auth_header = request.headers.get("Authorization", "")
    if auth_header == f"Bearer {configured_token}":
        return None

    return {"error": "Unauthorized."}, 401


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


@auth_bp.get("/login")
def login_form():
    return render_template("login.html")


@auth_bp.post("/login")
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    session_factory = _get_session_factory()
    with session_factory() as db_session:
        user = db_session.execute(
            select(User).where(User.username == username, User.is_active.is_(True))
        ).scalar_one_or_none()

        if user is None or not check_password_hash(user.password_hash, password):
            flash("Ungültige Zugangsdaten", "error")
            return redirect(url_for("auth.login_form"))

        session["user"] = {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "tenant_id": user.tenant_id,
        }

    flash("Login erfolgreich", "success")
    next_path = request.args.get("next", "")
    if next_path.startswith("/") and not next_path.startswith("//"):
        return redirect(next_path)
    return redirect(url_for("main.index"))


@auth_bp.post("/logout")
def logout():
    session.pop("user", None)
    flash("Abgemeldet", "success")
    return redirect(url_for("auth.login_form"))
