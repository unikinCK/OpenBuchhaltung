from __future__ import annotations

import hashlib
import secrets
import threading
import time
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
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
ROLE_SUPPORT = "Support"
WRITE_ROLES = {ROLE_ADMIN, ROLE_BUCHHALTER}


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def generate_api_token() -> str:
    return f"obk_{secrets.token_urlsafe(32)}"


def hash_api_token(token: str) -> str:
    """Deterministischer SHA-256-Hash für API-Tokens (indexierter Lookup).

    Tokens sind lange Zufallswerte — ein langsamer Passwort-Hash ist hier
    unnötig und würde den Lookup je Request über alle Benutzer erzwingen.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_user() -> dict | None:
    return session.get("user")


def current_api_user() -> dict | None:
    return getattr(g, "api_user", None)


def current_api_tenant_id() -> int | None:
    user = current_api_user()
    if user is None:
        return None
    return user.get("tenant_id")


def api_has_global_access() -> bool:
    return bool(getattr(g, "api_global_access", False))


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
    """before_request-Hook für die API: Bearer-Token oder Session-Login.

    API-Auth ist standardmäßig aktiv (``API_REQUIRE_AUTH``, Default an); für
    lokale Entwicklung kann sie per ``API_REQUIRE_AUTH=0`` abgeschaltet werden.
    Akzeptiert werden der globale ``API_AUTH_TOKEN`` oder ein Benutzer-API-Token
    (SHA-256-Lookup). Ohne Bearer-Header erhalten eingeloggte UI-Sessions
    lesenden Zugriff (GET/HEAD) im eigenen Tenant-Scope — dafür sind z. B. die
    CSV-Downloadlinks der Berichte-Seite gedacht.
    """
    if request.endpoint == "api.health":
        return None

    configured_token = current_app.config.get("API_AUTH_TOKEN")
    require_auth = bool(configured_token) or current_app.config.get("API_REQUIRE_AUTH", True)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        session_user = current_user()
        if session_user is not None and request.method in {"GET", "HEAD", "OPTIONS"}:
            g.api_user = dict(session_user)
            g.api_global_access = session_user.get("tenant_id") is None
            return None
        if require_auth:
            return {"error": "Unauthorized."}, 401
        return None

    token = auth_header.removeprefix("Bearer ").strip()
    if configured_token and secrets.compare_digest(token, configured_token):
        g.api_global_access = True
        return None

    api_user = _lookup_user_by_api_token(token)
    if api_user is not None:
        g.api_user = api_user
        g.api_global_access = api_user["tenant_id"] is None
        return None

    return {"error": "Unauthorized."}, 401


def _lookup_user_by_api_token(token: str) -> dict | None:
    """Sucht den aktiven Benutzer zu einem API-Token.

    Neue Tokens sind als SHA-256 gespeichert und werden per eindeutigem Index
    nachgeschlagen. Alt-Tokens (werkzeug-Passwort-Hashes, erkennbar am ``$``)
    werden einmalig verifiziert und beim ersten Treffer auf SHA-256 migriert,
    damit der teure Scan über alle Benutzer entfällt.
    """
    digest = hash_api_token(token)
    session_factory = _get_session_factory()
    with session_factory() as db_session:
        user = db_session.execute(
            select(User).where(User.api_token_hash == digest)
        ).scalar_one_or_none()
        if user is not None:
            return _api_user_dict(user) if user.is_active else None

        legacy_users = (
            db_session.execute(
                select(User).where(User.api_token_hash.contains("$"))
            )
            .scalars()
            .all()
        )
        for legacy_user in legacy_users:
            if check_password_hash(legacy_user.api_token_hash, token):
                if not legacy_user.is_active:
                    return None
                legacy_user.api_token_hash = digest
                db_session.commit()
                return _api_user_dict(legacy_user)
    return None


def _api_user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
    }


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


# Fehlversuchszähler für das Login-Rate-Limit: (remote_addr, username) -> Zeitstempel.
_failed_logins: dict[tuple[str, str], list[float]] = {}
_failed_logins_lock = threading.Lock()


def reset_login_rate_limiter() -> None:
    """Setzt alle Fehlversuchszähler zurück (für Tests)."""
    with _failed_logins_lock:
        _failed_logins.clear()


def _login_rate_key(username: str) -> tuple[str, str]:
    return (request.remote_addr or "unknown", username.lower())


def _login_blocked(key: tuple[str, str]) -> bool:
    window = current_app.config.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 900)
    max_attempts = current_app.config.get("LOGIN_RATE_LIMIT_ATTEMPTS", 5)
    now = time.monotonic()
    with _failed_logins_lock:
        attempts = [stamp for stamp in _failed_logins.get(key, []) if now - stamp < window]
        if attempts:
            _failed_logins[key] = attempts
        else:
            _failed_logins.pop(key, None)
        return len(attempts) >= max_attempts


def _register_failed_login(key: tuple[str, str]) -> None:
    with _failed_logins_lock:
        _failed_logins.setdefault(key, []).append(time.monotonic())


def _reset_failed_logins(key: tuple[str, str]) -> None:
    with _failed_logins_lock:
        _failed_logins.pop(key, None)


@auth_bp.get("/login")
def login_form():
    return render_template("login.html")


@auth_bp.post("/login")
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    rate_limit_active = current_app.config.get("LOGIN_RATE_LIMIT", True)
    rate_key = _login_rate_key(username)
    if rate_limit_active and _login_blocked(rate_key):
        flash(
            "Zu viele fehlgeschlagene Anmeldeversuche. Bitte später erneut versuchen.",
            "error",
        )
        return render_template("login.html"), 429

    session_factory = _get_session_factory()
    with session_factory() as db_session:
        user = db_session.execute(
            select(User).where(User.username == username, User.is_active.is_(True))
        ).scalar_one_or_none()

        if user is None or not check_password_hash(user.password_hash, password):
            if rate_limit_active:
                _register_failed_login(rate_key)
            flash("Ungültige Zugangsdaten", "error")
            return redirect(url_for("auth.login_form"))

        if rate_limit_active:
            _reset_failed_logins(rate_key)

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
