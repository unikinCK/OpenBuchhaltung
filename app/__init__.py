from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from .api import api_bp
from .auth import auth_bp, ensure_csrf_token
from .cli import register_cli_commands
from .db import create_session_factory
from .main import main_bp


def create_app(test_config: dict | None = None) -> Flask:
    """Application factory for OpenBuchhaltung."""
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-me"),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        MCP_SERVER_URL=os.environ.get("MCP_SERVER_URL"),
        DOCUMENT_UPLOAD_DIR=str(Path(app.instance_path) / "uploads"),
        DOCUMENT_LLM_ENDPOINT_URL=os.environ.get("DOCUMENT_LLM_ENDPOINT_URL"),
        DOCUMENT_LLM_MODEL=os.environ.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini"),
        API_AUTH_TOKEN=os.environ.get("API_AUTH_TOKEN"),
        API_REQUIRE_AUTH=os.environ.get("API_REQUIRE_AUTH", "0") == "1",
        CSRF_PROTECT=os.environ.get("CSRF_PROTECT", "1") == "1",
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

    return app
