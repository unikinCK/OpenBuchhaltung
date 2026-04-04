from __future__ import annotations

from flask import Flask

from .api import api_bp
from .auth import auth_bp
from .db import create_session_factory
from .main import main_bp


def create_app(test_config: dict | None = None) -> Flask:
    """Application factory for OpenBuchhaltung."""
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY="dev-secret-key-change-me",
        DATABASE_URL=None,
        MCP_SERVER_URL=None,
    )

    if test_config:
        app.config.update(test_config)

    app.extensions["db_session_factory"] = create_session_factory(app.config.get("DATABASE_URL"))

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)

    return app
