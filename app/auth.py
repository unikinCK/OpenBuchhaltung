from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlsplit

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
from sqlalchemy import delete, func, select
from werkzeug.security import check_password_hash, generate_password_hash

from app.services.security_events import record_security_event
from domain.models import LoginAttempt, User

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.before_request
def _protect_auth_posts():
    validate_csrf()


ROLE_ADMIN = "Admin"
ROLE_BUCHHALTER = "Buchhalter"
ROLE_PRUEFER = "Pruefer"
ROLE_SUPPORT = "Support"
WRITE_ROLES = {ROLE_ADMIN, ROLE_BUCHHALTER}

MIN_PASSWORD_LENGTH = 8

# Bei unbekanntem Benutzernamen wird gegen diesen Hash geprüft, damit die
# Antwortzeit keinen Rückschluss auf die Existenz des Kontos erlaubt.
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def password_policy_error(password: str) -> str | None:
    """Liefert die Verletzung der Passwortrichtlinie oder None."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein."
    return None


def constant_time_equals(left: str, right: str) -> bool:
    """Zeitkonstanter Vergleich, der auch Nicht-ASCII-Eingaben verträgt.

    ``secrets.compare_digest`` wirft bei Nicht-ASCII-Strings einen ``TypeError``
    (HTTP 500); die Byte-Variante vergleicht beliebige Eingaben.
    """
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def generate_api_token() -> str:
    return f"obk_{secrets.token_urlsafe(32)}"


def hash_api_token(token: str) -> str:
    """Deterministischer SHA-256-Hash für API-Tokens (indexierter Lookup).

    Tokens sind lange Zufallswerte — ein langsamer Passwort-Hash ist hier
    unnötig und würde den Lookup je Request über alle Benutzer erzwingen.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def current_user() -> dict | None:
    """Return the current, database-backed UI user.

    The session only carries the user id as a lookup hint. Role, tenant scope and
    active status are refreshed once per request so administrative changes take
    effect immediately for already logged-in browsers.
    """
    if getattr(g, "_current_user_loaded", False):
        return getattr(g, "_current_user", None)

    g._current_user_loaded = True
    session_user = session.get("user")
    if not isinstance(session_user, dict) or not session_user.get("id"):
        g._current_user = None
        return None

    session_factory = _get_session_factory()
    with session_factory() as db_session:
        user = db_session.get(User, session_user["id"])
        if user is None or not user.is_active:
            session.pop("user", None)
            g._current_user = None
            return None
        refreshed_user = _api_user_dict(user)

    if session_user != refreshed_user:
        session["user"] = refreshed_user
    g._current_user = refreshed_user
    return refreshed_user


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
    if not token or not submitted or not constant_time_equals(token, submitted):
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

    # Interne In-Process-Aufrufe (KI-Chat-Tools, MCP-Bridge) übergeben den
    # Auth-Kontext über einen WSGI-environ-Eintrag. Externe Clients können nur
    # HTTP_*-Schlüssel setzen, diesen Eintrag also nicht fälschen.
    internal_context = request.environ.get("openbuchhaltung.internal_api")
    if isinstance(internal_context, dict):
        internal_user = internal_context.get("user")
        if isinstance(internal_user, dict):
            g.api_user = dict(internal_user)
        g.api_global_access = bool(internal_context.get("global_access"))
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
    if configured_token and constant_time_equals(token, configured_token):
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


# ---------------------------------------------------------------------------
# Login-Rate-Limit (Fehlversuche in der DB, damit es über Worker/Prozesse hinweg
# und hinter einem Reverse-Proxy je Client-Adresse gilt)
# ---------------------------------------------------------------------------


def _login_attempt_key(username: str) -> str:
    return (username or "").strip().lower()[:120]


def _login_window_cutoff() -> datetime:
    window = int(current_app.config.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 900))
    return datetime.now(timezone.utc) - timedelta(seconds=window)


def login_blocked(db_session, *, username: str, remote_addr: str) -> bool:
    """Ob (Benutzername, Client-Adresse) im Zeitfenster zu viele Fehlversuche hat."""
    max_attempts = int(current_app.config.get("LOGIN_RATE_LIMIT_ATTEMPTS", 5))
    count = db_session.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.username == _login_attempt_key(username),
            LoginAttempt.remote_addr == remote_addr,
            LoginAttempt.attempted_at >= _login_window_cutoff(),
        )
    )
    return (count or 0) >= max_attempts


def register_failed_login(db_session, *, username: str, remote_addr: str) -> None:
    """Zählt einen Fehlversuch und räumt abgelaufene Einträge auf."""
    db_session.execute(
        delete(LoginAttempt).where(LoginAttempt.attempted_at < _login_window_cutoff())
    )
    db_session.add(
        LoginAttempt(
            username=_login_attempt_key(username),
            remote_addr=remote_addr[:64],
            attempted_at=datetime.now(timezone.utc),
        )
    )


def reset_failed_logins(db_session, *, username: str, remote_addr: str) -> None:
    db_session.execute(
        delete(LoginAttempt).where(
            LoginAttempt.username == _login_attempt_key(username),
            LoginAttempt.remote_addr == remote_addr,
        )
    )


def unlock_user_login(db_session, *, username: str) -> int:
    """Admin-Entsperrung: löscht alle Fehlversuche eines Benutzernamens."""
    result = db_session.execute(
        delete(LoginAttempt).where(LoginAttempt.username == _login_attempt_key(username))
    )
    return int(result.rowcount or 0)


def safe_next_path(candidate: str | None) -> str | None:
    """Erlaubt als ``next``-Ziel nur relative Pfade dieser Anwendung.

    Abgelehnt werden absolute URLs, protokollrelative Ziele (``//host``),
    Backslashes (Browser normalisieren ``/\\host`` zu ``//host``) sowie
    Steuer- und Leerzeichen.
    """
    if not candidate:
        return None
    if "\\" in candidate or any(ch.isspace() or ord(ch) < 32 for ch in candidate):
        return None
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return None
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        return None
    return candidate


# ---------------------------------------------------------------------------
# Login / Logout / Passwort
# ---------------------------------------------------------------------------


@auth_bp.get("/login")
def login_form():
    return render_template("login.html")


@auth_bp.post("/login")
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    remote_addr = request.remote_addr or "unknown"
    rate_limit_active = current_app.config.get("LOGIN_RATE_LIMIT", True)
    event_payload = {"remote_addr": remote_addr}

    session_factory = _get_session_factory()
    with session_factory() as db_session:
        user = db_session.execute(
            select(User).where(User.username == username, User.is_active.is_(True))
        ).scalar_one_or_none()

        if rate_limit_active and login_blocked(
            db_session, username=username, remote_addr=remote_addr
        ):
            # Nur ins App-Log: gesperrte Versuche sind beliebig oft auslösbar und
            # sollen die Audit-Hashkette nicht fluten.
            record_security_event(
                db_session,
                user=user,
                action="login_blocked",
                actor=username or "-",
                payload=event_payload,
                audit=False,
            )
            flash(
                "Zu viele fehlgeschlagene Anmeldeversuche. Bitte später erneut versuchen.",
                "error",
            )
            return render_template("login.html"), 429

        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        if user is None or not check_password_hash(password_hash, password):
            if rate_limit_active:
                register_failed_login(db_session, username=username, remote_addr=remote_addr)
            record_security_event(
                db_session,
                user=user,
                action="login_failed",
                actor=username or "-",
                payload=event_payload,
            )
            db_session.commit()
            flash("Ungültige Zugangsdaten", "error")
            return redirect(url_for("auth.login_form"))

        if rate_limit_active:
            reset_failed_logins(db_session, username=username, remote_addr=remote_addr)
        record_security_event(
            db_session, user=user, action="login", actor=user.username, payload=event_payload
        )
        db_session.commit()
        session_user = _api_user_dict(user)

    # Neue Session je Login: verhindert Session-Fixation und rotiert den
    # CSRF-Token; ``permanent`` aktiviert die konfigurierte Session-Laufzeit
    # (PERMANENT_SESSION_LIFETIME wirkt als Idle-Timeout).
    session.clear()
    session["user"] = session_user
    session.permanent = True

    flash("Login erfolgreich", "success")
    next_path = safe_next_path(request.args.get("next", ""))
    return redirect(next_path or url_for("main.index"))


@auth_bp.post("/logout")
def logout():
    session.clear()
    flash("Abgemeldet", "success")
    return redirect(url_for("auth.login_form"))


@auth_bp.get("/password")
@login_required
def password_form():
    return render_template("password.html", companies=[], selected_company_id=None)


@auth_bp.post("/password")
@login_required
def change_password():
    """Selbstbedienung: eigenes Passwort ändern (aktuelles Passwort erforderlich)."""
    user = current_user()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    error = password_policy_error(new_password)
    if error is None and new_password != confirm_password:
        error = "Die Wiederholung stimmt nicht mit dem neuen Passwort überein."

    session_factory = _get_session_factory()
    with session_factory() as db_session:
        db_user = db_session.get(User, user["id"])
        if db_user is None or not check_password_hash(db_user.password_hash, current_password):
            record_security_event(
                db_session,
                user=db_user,
                action="password_change_failed",
                actor=user["username"],
                audit=False,
            )
            flash("Das aktuelle Passwort ist falsch.", "error")
            return redirect(url_for("auth.password_form"))
        if error is not None:
            flash(error, "error")
            return redirect(url_for("auth.password_form"))
        db_user.password_hash = hash_password(new_password)
        record_security_event(
            db_session, user=db_user, action="password_changed", actor=db_user.username
        )
        db_session.commit()

    flash("Passwort wurde geändert.", "success")
    return redirect(url_for("main.index"))
