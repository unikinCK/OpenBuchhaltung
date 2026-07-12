from __future__ import annotations

import os
from pathlib import Path

from flask import Flask
from werkzeug.exceptions import RequestEntityTooLarge

from .api import api_bp
from .auth import auth_bp, ensure_csrf_token
from .cli import register_cli_commands
from .db import create_session_factory
from .web import main_bp


def create_app(test_config: dict | None = None) -> Flask:
    """Application factory for OpenBuchhaltung."""
    document_max_upload_bytes = int(os.environ.get("DOCUMENT_MAX_UPLOAD_BYTES", "10485760"))
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        MCP_SERVER_URL=os.environ.get("MCP_SERVER_URL"),
        DOCUMENT_UPLOAD_DIR=str(Path(app.instance_path) / "uploads"),
        DOCUMENT_MAX_UPLOAD_BYTES=document_max_upload_bytes,
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
        API_AUTH_TOKEN=os.environ.get("API_AUTH_TOKEN"),
        API_REQUIRE_AUTH=os.environ.get("API_REQUIRE_AUTH", "0") == "1",
        CSRF_PROTECT=os.environ.get("CSRF_PROTECT", "1") == "1",
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
        # Tests posten Formulare ohne Token; expliziter Opt-in via CSRF_PROTECT möglich.
        if app.config.get("TESTING") and "CSRF_PROTECT" not in test_config:
            app.config["CSRF_PROTECT"] = False

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
            "script-src 'self' 'unsafe-inline'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_large_upload(exc):
        del exc
        return {"error": "Uploaded file is too large."}, 413

    return app
