from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from .api import api_bp
from .auth import auth_bp, ensure_csrf_token
from .cli import register_cli_commands
from .db import create_session_factory
from .web import main_bp

logger = logging.getLogger(__name__)
INSECURE_DEVELOPMENT_SECRET = "dev-secret-key-change-me"
DEVELOPMENT_ENVIRONMENTS = {"dev", "development", "local"}
DEFAULT_SESSION_LIFETIME_SECONDS = 8 * 60 * 60
DEFAULT_HSTS_MAX_AGE = 365 * 24 * 60 * 60


def _env_flag(name: str) -> bool | None:
    """Liest ein Ja/Nein-Flag aus der Umgebung; None, wenn nicht gesetzt."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return int(raw)


def is_production(app: Flask) -> bool:
    """Produktionsbetrieb = weder Tests noch explizite Entwicklungsumgebung."""
    if app.config.get("TESTING"):
        return False
    environment = str(app.config.get("APP_ENV") or "production").strip().lower()
    return environment not in DEVELOPMENT_ENVIRONMENTS


def create_app(test_config: dict | None = None) -> Flask:
    """Application factory for OpenBuchhaltung."""
    document_max_upload_bytes = int(os.environ.get("DOCUMENT_MAX_UPLOAD_BYTES", "10485760"))
    document_min_upload_bytes = int(os.environ.get("DOCUMENT_MIN_UPLOAD_BYTES", "1024"))
    app = Flask(__name__)
    app.config.from_mapping(
        APP_ENV=os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "production",
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # None = Vorgabe nach Umgebung (Produktion: an); siehe _configure_hardening.
        SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE"),
        # Idle-Timeout der Browser-Session (Login setzt session.permanent).
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=int(
                os.environ.get("SESSION_LIFETIME_SECONDS", str(DEFAULT_SESSION_LIFETIME_SECONDS))
            )
        ),
        # Anzahl vertrauenswürdiger Reverse-Proxies (X-Forwarded-*); None = Vorgabe
        # nach Umgebung (Produktion: 1, sonst 0).
        TRUSTED_PROXY_COUNT=_env_int("TRUSTED_PROXY_COUNT"),
        HSTS_ENABLED=_env_flag("HSTS_ENABLED"),
        HSTS_MAX_AGE=int(os.environ.get("HSTS_MAX_AGE", str(DEFAULT_HSTS_MAX_AGE))),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        DOCUMENT_UPLOAD_DIR=str(Path(app.instance_path) / "uploads"),
        DOCUMENT_MAX_UPLOAD_BYTES=document_max_upload_bytes,
        DOCUMENT_MIN_UPLOAD_BYTES=document_min_upload_bytes,
        MAX_CONTENT_LENGTH=document_max_upload_bytes,
        DOCUMENT_LLM_ENDPOINT_URL=os.environ.get("DOCUMENT_LLM_ENDPOINT_URL"),
        DOCUMENT_LLM_MODEL=os.environ.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini"),
        # OCR-Pipeline: fällt auf den Beleg-LLM-Endpoint zurück, wenn nicht separat gesetzt.
        RECEIPT_OCR_ENDPOINT_URL=os.environ.get("RECEIPT_OCR_ENDPOINT_URL")
        or os.environ.get("DOCUMENT_LLM_ENDPOINT_URL"),
        RECEIPT_OCR_MODEL=os.environ.get(
            "RECEIPT_OCR_MODEL", os.environ.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini")
        ),
        # LLM zur Feld-Extraktion (Unterstützung/Fallback) und Kontrolle des
        # regelbasierten Vorschlags; Fallback-Kette auf den Beleg-LLM-Endpoint.
        RECEIPT_LLM_ENDPOINT_URL=os.environ.get("RECEIPT_LLM_ENDPOINT_URL")
        or os.environ.get("DOCUMENT_LLM_ENDPOINT_URL"),
        RECEIPT_LLM_MODEL=os.environ.get(
            "RECEIPT_LLM_MODEL", os.environ.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini")
        ),
        # LLM für den Belegabgleich (Beleg ↔ vorhandene Buchungen);
        # Fallback-Kette über den Beleg-Extraktions- auf den Beleg-LLM-Endpoint.
        RECEIPT_MATCH_LLM_ENDPOINT_URL=os.environ.get("RECEIPT_MATCH_LLM_ENDPOINT_URL")
        or os.environ.get("RECEIPT_LLM_ENDPOINT_URL")
        or os.environ.get("DOCUMENT_LLM_ENDPOINT_URL"),
        RECEIPT_MATCH_LLM_MODEL=os.environ.get(
            "RECEIPT_MATCH_LLM_MODEL",
            os.environ.get(
                "RECEIPT_LLM_MODEL", os.environ.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini")
            ),
        ),
        # KI-Chat: OpenAI-/responses-kompatibler Endpoint für den integrierten
        # Chat mit Tool-Zugriff; Fallback auf den Beleg-LLM-Endpoint.
        CHAT_LLM_ENDPOINT_URL=os.environ.get("CHAT_LLM_ENDPOINT_URL")
        or os.environ.get("DOCUMENT_LLM_ENDPOINT_URL"),
        CHAT_LLM_MODEL=os.environ.get(
            "CHAT_LLM_MODEL", os.environ.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini")
        ),
        CHAT_LLM_API_KEY=os.environ.get("CHAT_LLM_API_KEY"),
        CHAT_LLM_MAX_TOOL_CALLS=int(os.environ.get("CHAT_LLM_MAX_TOOL_CALLS", "15")),
        CHAT_LLM_TIMEOUT_SECONDS=float(os.environ.get("CHAT_LLM_TIMEOUT_SECONDS", "120")),
        # FinTS-Produktkennung (Registrierung der Deutschen Kreditwirtschaft),
        # Voraussetzung für den Direktabruf von Bankumsätzen.
        FINTS_PRODUCT_ID=os.environ.get("FINTS_PRODUCT_ID"),
        API_AUTH_TOKEN=os.environ.get("API_AUTH_TOKEN"),
        # Default-secure: API-Auth ist aktiv, Opt-out für lokale Entwicklung
        # per API_REQUIRE_AUTH=0.
        API_REQUIRE_AUTH=os.environ.get("API_REQUIRE_AUTH", "1") == "1",
        CSRF_PROTECT=os.environ.get("CSRF_PROTECT", "1") == "1",
        LOGIN_RATE_LIMIT=os.environ.get("LOGIN_RATE_LIMIT", "1") == "1",
        LOGIN_RATE_LIMIT_ATTEMPTS=int(os.environ.get("LOGIN_RATE_LIMIT_ATTEMPTS", "5")),
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=int(
            os.environ.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "900")
        ),
        DATEV_CONSULTANT_NUMBER=int(os.environ.get("DATEV_CONSULTANT_NUMBER", "1000")),
        DATEV_CLIENT_NUMBER=(
            int(os.environ["DATEV_CLIENT_NUMBER"])
            if os.environ.get("DATEV_CLIENT_NUMBER")
            else None
        ),
        SELLER_STREET=os.environ.get("SELLER_STREET", ""),
        SELLER_POSTAL_CODE=os.environ.get("SELLER_POSTAL_CODE", ""),
        SELLER_CITY=os.environ.get("SELLER_CITY", ""),
        SELLER_COUNTRY_CODE=os.environ.get("SELLER_COUNTRY_CODE", "DE"),
        SELLER_VAT_ID=os.environ.get("SELLER_VAT_ID", ""),
    )

    if test_config:
        app.config.update(test_config)
        # Tests posten Formulare ohne Token bzw. rufen die API ohne Bearer auf;
        # expliziter Opt-in je Test über die jeweilige Config möglich.
        if app.config.get("TESTING"):
            if "CSRF_PROTECT" not in test_config:
                app.config["CSRF_PROTECT"] = False
            if "API_REQUIRE_AUTH" not in test_config:
                app.config["API_REQUIRE_AUTH"] = False
            if "LOGIN_RATE_LIMIT" not in test_config:
                app.config["LOGIN_RATE_LIMIT"] = False

    _configure_secret_key(app)
    _configure_hardening(app)

    Path(app.config["DOCUMENT_UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    app.extensions["db_session_factory"] = create_session_factory(app.config.get("DATABASE_URL"))

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)
    register_cli_commands(app)

    @app.context_processor
    def _inject_csrf_token():
        return {"csrf_token": ensure_csrf_token}

    @app.after_request
    def _add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )
        # HSTS nur über HTTPS senden (Browser ignorieren es sonst ohnehin);
        # hinter ProxyFix zählt X-Forwarded-Proto.
        if app.config.get("HSTS_ENABLED") and request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={int(app.config.get('HSTS_MAX_AGE') or DEFAULT_HSTS_MAX_AGE)}; "
                "includeSubDomains",
            )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_large_upload(exc):
        del exc
        return {"error": "Uploaded file is too large."}, 413

    return app


def _configure_secret_key(app: Flask) -> None:
    """Require an explicit session secret except in tests or an explicit dev environment."""
    if app.config.get("TESTING"):
        app.config["SECRET_KEY"] = app.config.get("SECRET_KEY") or "testing-secret-key"
        return

    environment = str(app.config.get("APP_ENV") or "production").strip().lower()
    secret_key = app.config.get("SECRET_KEY")
    if environment in DEVELOPMENT_ENVIRONMENTS:
        if not secret_key:
            app.config["SECRET_KEY"] = INSECURE_DEVELOPMENT_SECRET
            logger.warning(
                "Kein SECRET_KEY gesetzt; die unsichere Entwicklungsvorgabe wird verwendet."
            )
        return

    if not secret_key or secret_key == INSECURE_DEVELOPMENT_SECRET:
        raise RuntimeError(
            "SECRET_KEY muss außerhalb einer expliziten Entwicklungsumgebung gesetzt sein. "
            "Für lokale Entwicklung APP_ENV=development setzen."
        )


def _configure_hardening(app: Flask) -> None:
    """Produktionsvorgaben für Cookie-Secure-Flag, HSTS und ProxyFix.

    In Produktion (``APP_ENV`` weder Test- noch Entwicklungsumgebung) sind das
    Secure-Flag der Session-Cookies und HSTS eingeschaltet und genau ein
    Reverse-Proxy (Caddy, Tailscale Serve) wird für ``X-Forwarded-For/-Proto``
    als vertrauenswürdig behandelt. Alle drei Vorgaben lassen sich explizit
    überschreiben (``SESSION_COOKIE_SECURE``, ``HSTS_ENABLED``,
    ``TRUSTED_PROXY_COUNT``); ``SESSION_COOKIE_SECURE=0`` in Produktion wird
    protokolliert, weil Sessions dann über Klartext-HTTP abgreifbar sind.
    """
    production = is_production(app)

    if app.config.get("SESSION_COOKIE_SECURE") is None:
        app.config["SESSION_COOKIE_SECURE"] = production
    elif production and not app.config["SESSION_COOKIE_SECURE"]:
        logger.warning(
            "SESSION_COOKIE_SECURE=0 in Produktion: Session-Cookies werden auch über "
            "unverschlüsseltes HTTP gesendet."
        )

    if app.config.get("HSTS_ENABLED") is None:
        app.config["HSTS_ENABLED"] = production

    if app.config.get("TRUSTED_PROXY_COUNT") is None:
        app.config["TRUSTED_PROXY_COUNT"] = 1 if production else 0
    proxies = int(app.config["TRUSTED_PROXY_COUNT"])
    if proxies > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)
