"""WSGI-Einstieg für gunicorn: ``gunicorn push_relay.wsgi:app`` (genau ein Worker)."""

from __future__ import annotations

import logging
import os

from push_relay.app import create_app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = create_app()
