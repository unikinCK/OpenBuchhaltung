from __future__ import annotations

import hashlib
import logging
import secrets
import time
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

from app.services.mcp_server import MCP_CHANNEL, MCP_CHANNEL_HEADER
from app.services.security_events import record_security_event
from app.services.two_factor import (
    TwoFactorBlockedError,
    TwoFactorCredentialsError,
    TwoFactorError,
    confirm_setup,
    disable_two_factor,
    group_secret,
    pending_setup_secret,
    provisioning_uri_for,
    qr_code_svg,
    record_verified_login,
    regenerate_recovery_codes,
    start_setup,
    two_factor_enabled,
    two_factor_required_for,
    two_factor_status,
    verify_code,
)
from domain.models import LoginAttempt, User

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.before_request
def _protect_auth_posts():
    validate_csrf()
    if request.endpoint not in _OPEN_DURING_TWO_FACTOR_SETUP and two_factor_setup_required():
        return redirect_to_two_factor_setup()
    return None


ROLE_ADMIN = "Admin"
ROLE_BUCHHALTER = "Buchhalter"
ROLE_PRUEFER = "Pruefer"
ROLE_SUPPORT = "Support"
WRITE_ROLES = {ROLE_ADMIN, ROLE_BUCHHALTER}

MIN_PASSWORD_LENGTH = 8

# Zwischenstand zwischen Passwort und zweitem Faktor: nur diese Daten, kein ``session["user"]``.
TWO_FACTOR_PENDING_KEY = "two_factor_pending"
TWO_FACTOR_PENDING_SECONDS = 300
TWO_FACTOR_SETUP_NEXT_KEY = "two_factor_setup_next"
# Das Sprungziel liegt im Session-Cookie, das Browser nur bis rund 4 KB annehmen.
MAX_STORED_NEXT_LENGTH = 2000
# Fehlversuche beim zweiten Faktor zählen zusätzlich je Benutzername über alle Adressen:
# Wer das Passwort kennt, soll Codes nicht von vielen Adressen aus durchprobieren können.
TWO_FACTOR_ATTEMPT_ADDR = "two-factor"
# Bleiben erreichbar, solange die Einrichtung für die Rolle vorgeschrieben, aber offen ist.
_OPEN_DURING_TWO_FACTOR_SETUP = {
    "auth.login_form",
    "auth.login",
    "auth.second_factor_form",
    "auth.second_factor",
    "auth.logout",
    "auth.two_factor_page",
    "auth.two_factor_setup",
    "auth.two_factor_confirm",
}

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
        g._two_factor_setup_required = (
            not two_factor_enabled(user) and two_factor_required_for(user.role)
        )

    if session_user != refreshed_user:
        session["user"] = refreshed_user
    g._current_user = refreshed_user
    return refreshed_user


def two_factor_setup_required() -> bool:
    """Ob die Rolle des angemeldeten Benutzers 2FA verlangt, die noch nicht eingerichtet ist."""
    return current_user() is not None and getattr(g, "_two_factor_setup_required", False)


def redirect_to_two_factor_setup():
    """Leitet zur Einrichtung um und merkt sich das ursprüngliche Ziel eines Seitenaufrufs."""
    if request.method == "GET":
        target = _storable_next_path(request.full_path.rstrip("?"))
        if target:
            session[TWO_FACTOR_SETUP_NEXT_KEY] = target
    return redirect(url_for("auth.two_factor_page"))


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

    if two_factor_setup_required():
        return redirect_to_two_factor_setup()

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
    # Download-Links tragen ihre Berechtigung im signierten Token (app/api/downloads.py).
    if request.endpoint in {"api.health", "api.download_via_link"}:
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
        g.api_channel = internal_context.get("channel")
        return None

    # Aufrufe über den MCP-Server tragen einen Kanal-Header. Er kann nur einschränken
    # (z. B. Firmenwissen ohne MCP-Freigabe), nie Rechte geben – fälschen ist also egal.
    channel = request.headers.get(MCP_CHANNEL_HEADER, "").strip().lower()
    g.api_channel = MCP_CHANNEL if channel == MCP_CHANNEL else None

    configured_token = current_app.config.get("API_AUTH_TOKEN")
    require_auth = bool(configured_token) or current_app.config.get("API_REQUIRE_AUTH", True)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        session_user = current_user()
        if session_user is not None and request.method in {"GET", "HEAD", "OPTIONS"}:
            if two_factor_setup_required():
                return {"error": "Zwei-Faktor-Anmeldung muss zuerst eingerichtet werden."}, 403
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

    api_user = _lookup_user_by_api_token(token) or _lookup_user_by_oauth_token(token)
    if api_user is not None:
        g.api_user = api_user
        g.api_global_access = api_user["tenant_id"] is None
        return None

    return {"error": "Unauthorized."}, 401


def _lookup_user_by_oauth_token(token: str) -> dict | None:
    """Benutzer zu einem OAuth-Access-Token (MCP-Connectoren wie ChatGPT)."""
    if not current_app.config.get("OAUTH_ENABLED", True):
        return None
    from app.oauth import lookup_user_by_access_token  # Zyklus auth <-> oauth vermeiden

    with _get_session_factory()() as db_session:
        user = lookup_user_by_access_token(db_session, token)
        return _api_user_dict(user) if user is not None else None


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


def active_api_user(user_id: int) -> dict | None:
    """API-Benutzerdaten eines aktiven Benutzers (Rolle und Mandant frisch aus der DB)."""
    with _get_session_factory()() as db_session:
        user = db_session.get(User, user_id)
        return _api_user_dict(user) if user is not None and user.is_active else None


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


def second_factor_blocked(db_session, *, username: str, remote_addr: str) -> bool:
    return any(
        login_blocked(db_session, username=username, remote_addr=addr)
        for addr in (remote_addr, TWO_FACTOR_ATTEMPT_ADDR)
    )


def register_failed_second_factor(db_session, *, username: str, remote_addr: str) -> None:
    for addr in (remote_addr, TWO_FACTOR_ATTEMPT_ADDR):
        register_failed_login(db_session, username=username, remote_addr=addr)


def reset_failed_second_factor(db_session, *, username: str, remote_addr: str) -> None:
    for addr in (remote_addr, TWO_FACTOR_ATTEMPT_ADDR):
        reset_failed_logins(db_session, username=username, remote_addr=addr)


def run_guarded_two_factor_action(db_session, user: User, action):
    """Führt eine Aktion mit Passwort und Code aus; Fehlversuche zählen im Rate-Limit.

    Ohne Grenze ließen sich Passwort oder Code mit einer fremden Session oder einem
    abgeflossenen API-Token durchprobieren.
    """
    limit_active = current_app.config.get("LOGIN_RATE_LIMIT", True)
    remote_addr = request.remote_addr or "unknown"
    if limit_active and second_factor_blocked(
        db_session, username=user.username, remote_addr=remote_addr
    ):
        raise TwoFactorBlockedError("Zu viele Fehlversuche. Bitte später erneut versuchen.")
    try:
        return action()
    except TwoFactorCredentialsError:
        if limit_active:
            register_failed_second_factor(
                db_session, username=user.username, remote_addr=remote_addr
            )
            db_session.commit()
        raise


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


def _storable_next_path(candidate: str | None) -> str | None:
    """Geprüftes Sprungziel, das in die Session passt (zu lange Ziele entfallen)."""
    next_path = safe_next_path(candidate)
    if next_path and len(next_path) <= MAX_STORED_NEXT_LENGTH:
        return next_path
    return None


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

        if two_factor_enabled(user):
            # Die Fehlversuche bleiben stehen: Würde das richtige Passwort sie löschen,
            # ließen sich Codes durch erneutes Anmelden beliebig oft durchprobieren.
            record_security_event(
                db_session,
                user=user,
                action="two_factor_challenge",
                actor=user.username,
                payload=event_payload,
                audit=False,
            )
            db_session.commit()
            pending = {
                "user_id": user.id,
                "next": _storable_next_path(request.args.get("next", "")),
                "expires_at": int(time.time()) + TWO_FACTOR_PENDING_SECONDS,
            }
            session.clear()
            session[TWO_FACTOR_PENDING_KEY] = pending
            return redirect(url_for("auth.second_factor_form"))

        if rate_limit_active:
            reset_failed_logins(db_session, username=username, remote_addr=remote_addr)
        record_security_event(
            db_session, user=user, action="login", actor=user.username, payload=event_payload
        )
        db_session.commit()
        session_user = _api_user_dict(user)

    return _complete_login(session_user, request.args.get("next", ""))


def _complete_login(session_user: dict, next_path: str | None):
    # Neue Session je Login: verhindert Session-Fixation und rotiert den
    # CSRF-Token; ``permanent`` aktiviert die konfigurierte Session-Laufzeit
    # (PERMANENT_SESSION_LIFETIME wirkt als Idle-Timeout).
    session.clear()
    session["user"] = session_user
    session.permanent = True

    flash("Login erfolgreich", "success")
    return redirect(safe_next_path(next_path) or url_for("main.index"))


def _pending_second_factor() -> dict | None:
    """Gültiger Zwischenstand der Anmeldung; ein abgelaufener zählt nicht."""
    pending = session.get(TWO_FACTOR_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    if int(pending.get("expires_at") or 0) <= time.time():
        return None
    return pending


def _restart_login():
    """Zurück zum Passwort, das ursprüngliche Ziel (z. B. OAuth-Zustimmung) bleibt erhalten."""
    stale = session.pop(TWO_FACTOR_PENDING_KEY, None)
    next_path = stale.get("next") if isinstance(stale, dict) else None
    flash("Die Anmeldung ist abgelaufen. Bitte melden Sie sich erneut an.", "error")
    return redirect(url_for("auth.login_form", next=safe_next_path(next_path)))


@auth_bp.get("/login/zweiter-faktor")
def second_factor_form():
    if _pending_second_factor() is None:
        return _restart_login()
    return render_template("login_two_factor.html")


@auth_bp.post("/login/zweiter-faktor")
def second_factor():
    pending = _pending_second_factor()
    if pending is None:
        return _restart_login()

    remote_addr = request.remote_addr or "unknown"
    rate_limit_active = current_app.config.get("LOGIN_RATE_LIMIT", True)
    event_payload = {"remote_addr": remote_addr}
    remaining_codes = None

    with _get_session_factory()() as db_session:
        user = db_session.get(User, pending.get("user_id"))
        if user is None or not user.is_active or not two_factor_enabled(user):
            return _restart_login()
        username = user.username

        if rate_limit_active and second_factor_blocked(
            db_session, username=username, remote_addr=remote_addr
        ):
            record_security_event(
                db_session,
                user=user,
                action="two_factor_blocked",
                actor=username,
                payload=event_payload,
                audit=False,
            )
            flash(
                "Zu viele fehlgeschlagene Anmeldeversuche. Bitte später erneut versuchen.",
                "error",
            )
            return render_template("login_two_factor.html"), 429

        method = verify_code(db_session, user, request.form.get("code", ""))
        if method is None:
            if rate_limit_active:
                register_failed_second_factor(
                    db_session, username=username, remote_addr=remote_addr
                )
            record_security_event(
                db_session,
                user=user,
                action="two_factor_failed",
                actor=username,
                payload=event_payload,
            )
            db_session.commit()
            flash("Der Code ist ungültig oder wurde bereits verwendet.", "error")
            return redirect(url_for("auth.second_factor_form"))

        if rate_limit_active:
            reset_failed_second_factor(db_session, username=username, remote_addr=remote_addr)
        remaining_codes = record_verified_login(
            db_session, user, method=method, payload=event_payload
        )
        record_security_event(
            db_session, user=user, action="login", actor=username, payload=event_payload
        )
        db_session.commit()
        session_user = _api_user_dict(user)

    response = _complete_login(session_user, pending.get("next"))
    if remaining_codes is not None:
        flash(
            f"Wiederherstellungscode verwendet, noch {remaining_codes} übrig. "
            "Bei Bedarf erzeugen Sie unter Zwei-Faktor-Anmeldung neue Codes.",
            "warning",
        )
    return response


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


# ---------------------------------------------------------------------------
# Zwei-Faktor-Anmeldung: Einrichten, Abschalten, neue Wiederherstellungscodes
# ---------------------------------------------------------------------------


def _no_store(body):
    """Seiten mit Secret oder Codes dürfen nicht im Browser-Cache landen."""
    response = current_app.make_response(body)
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_recovery_codes(codes: list[str], *, continue_url: str):
    return _no_store(
        render_template(
            "two_factor_recovery_codes.html",
            recovery_codes=codes,
            continue_url=continue_url,
            companies=[],
            selected_company_id=None,
        )
    )


def _two_factor_error_redirect(exc: TwoFactorError):
    flash(str(exc), "error")
    return redirect(url_for("auth.two_factor_page"))


@auth_bp.get("/zwei-faktor")
@login_required
def two_factor_page():
    with _get_session_factory()() as db_session:
        db_user = db_session.get(User, current_user()["id"])
        secret = pending_setup_secret(db_user)
        return _no_store(
            render_template(
                "two_factor.html",
                status=two_factor_status(db_session, db_user),
                secret_grouped=group_secret(secret) if secret else None,
                qr_svg=qr_code_svg(provisioning_uri_for(db_user, secret)) if secret else None,
                companies=[],
                selected_company_id=None,
            )
        )


@auth_bp.post("/zwei-faktor/einrichten")
@login_required
def two_factor_setup():
    username = current_user()["username"]
    with _get_session_factory()() as db_session:
        db_user = db_session.get(User, current_user()["id"])
        try:
            run_guarded_two_factor_action(
                db_session,
                db_user,
                lambda: start_setup(
                    db_session,
                    db_user,
                    password=request.form.get("current_password", ""),
                    actor=username,
                ),
            )
        except TwoFactorError as exc:
            return _two_factor_error_redirect(exc)
        db_session.commit()
    return redirect(url_for("auth.two_factor_page"))


@auth_bp.post("/zwei-faktor/bestaetigen")
@login_required
def two_factor_confirm():
    username = current_user()["username"]
    with _get_session_factory()() as db_session:
        db_user = db_session.get(User, current_user()["id"])
        try:
            codes = confirm_setup(
                db_session, db_user, code=request.form.get("code", ""), actor=username
            )
        except TwoFactorError as exc:
            return _two_factor_error_redirect(exc)
        db_session.commit()
    # Die Pflicht ist erfüllt: Die Seite mit den Codes zeigt wieder die volle Navigation.
    g._two_factor_setup_required = False
    # Die Codes stehen direkt in der Antwort, nicht per flash(): Das läge im Session-Cookie.
    continue_url = safe_next_path(session.pop(TWO_FACTOR_SETUP_NEXT_KEY, None))
    return _render_recovery_codes(codes, continue_url=continue_url or url_for("main.index"))


@auth_bp.post("/zwei-faktor/abschalten")
@login_required
def two_factor_disable():
    username = current_user()["username"]
    with _get_session_factory()() as db_session:
        db_user = db_session.get(User, current_user()["id"])
        try:
            run_guarded_two_factor_action(
                db_session,
                db_user,
                lambda: disable_two_factor(
                    db_session,
                    db_user,
                    password=request.form.get("current_password", ""),
                    code=request.form.get("code", ""),
                    actor=username,
                ),
            )
        except TwoFactorError as exc:
            return _two_factor_error_redirect(exc)
        db_session.commit()
    flash("Die Zwei-Faktor-Anmeldung wurde abgeschaltet.", "success")
    return redirect(url_for("auth.two_factor_page"))


@auth_bp.post("/zwei-faktor/codes")
@login_required
def two_factor_recovery_codes():
    username = current_user()["username"]
    with _get_session_factory()() as db_session:
        db_user = db_session.get(User, current_user()["id"])
        try:
            codes = run_guarded_two_factor_action(
                db_session,
                db_user,
                lambda: regenerate_recovery_codes(
                    db_session,
                    db_user,
                    password=request.form.get("current_password", ""),
                    code=request.form.get("code", ""),
                    actor=username,
                ),
            )
        except TwoFactorError as exc:
            return _two_factor_error_redirect(exc)
        db_session.commit()
    return _render_recovery_codes(codes, continue_url=url_for("auth.two_factor_page"))


# ---------------------------------------------------------------------------
# Verbundene Apps (OAuth-Grants)
# ---------------------------------------------------------------------------


@auth_bp.get("/apps")
@login_required
def oauth_grants_page():
    from app.oauth import list_grants
    from app.services.push import list_devices, push_status

    user = current_user()
    show_all = user["role"] == ROLE_ADMIN and user.get("tenant_id") is None
    with _get_session_factory()() as db_session:
        grants = list_grants(db_session, user_id=None if show_all else user["id"])
        devices = list_devices(db_session, user_id=None if show_all else user["id"])
    return render_template(
        "oauth_grants.html",
        grants=grants,
        devices=devices,
        push=push_status(),
        show_all=show_all,
        companies=[],
        selected_company_id=None,
    )


def _managed_push_device(db_session, device_id: int):
    from domain.models import PushDevice

    user = current_user()
    may_manage_all = user["role"] == ROLE_ADMIN and user.get("tenant_id") is None
    device = db_session.get(PushDevice, device_id)
    if device is None or (device.user_id != user["id"] and not may_manage_all):
        abort(404)
    return device


@auth_bp.post("/apps/geraete/<int:device_id>/ereignisse")
@login_required
def update_push_device_events(device_id: int):
    from app.services.push import EVENTS, update_device

    events = [event for event in request.form.getlist("events") if event in EVENTS]
    with _get_session_factory()() as db_session:
        device = _managed_push_device(db_session, device_id)
        update_device(db_session, device, actor=current_user()["username"], events=events)
        db_session.commit()
    flash("Mitteilungen für das Gerät wurden gespeichert.", "success")
    return redirect(url_for("auth.oauth_grants_page"))


@auth_bp.post("/apps/geraete/<int:device_id>/test")
@login_required
def send_push_device_test(device_id: int):
    from app.services.push import PushError, enqueue_test_notification

    with _get_session_factory()() as db_session:
        device = _managed_push_device(db_session, device_id)
        try:
            enqueue_test_notification(db_session, device)
        except PushError as exc:
            flash(str(exc), "error")
            return redirect(url_for("auth.oauth_grants_page"))
        db_session.commit()
    flash("Testmitteilung ist unterwegs.", "success")
    return redirect(url_for("auth.oauth_grants_page"))


@auth_bp.post("/apps/geraete/<int:device_id>/widerrufen")
@login_required
def revoke_push_device(device_id: int):
    from app.services.push import revoke_device

    with _get_session_factory()() as db_session:
        device = _managed_push_device(db_session, device_id)
        if device.revoked_at is None:
            revoke_device(db_session, device, actor=current_user()["username"])
            db_session.commit()
    flash("Gerät wurde widerrufen.", "success")
    return redirect(url_for("auth.oauth_grants_page"))


@auth_bp.post("/apps/<int:grant_id>/revoke")
@login_required
def revoke_oauth_grant(grant_id: int):
    from app.oauth import revoke_grant
    from domain.models import OAuthGrant

    user = current_user()
    may_revoke_all = user["role"] == ROLE_ADMIN and user.get("tenant_id") is None
    with _get_session_factory()() as db_session:
        grant = db_session.get(OAuthGrant, grant_id)
        if grant is None or (grant.user_id != user["id"] and not may_revoke_all):
            abort(404)
        if grant.revoked_at is None:
            revoke_grant(db_session, grant, actor=user["username"])
            db_session.commit()
    flash("Zugriff wurde widerrufen.", "success")
    return redirect(url_for("auth.oauth_grants_page"))
