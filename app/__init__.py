from flask import Flask

from .auth import auth_bp
from .main import main_bp


def create_app() -> Flask:
    """Application factory for OpenBuchhaltung."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-secret-key-change-me"

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)

    return app
