"""Zentrale Logging-Konfiguration (``dictConfig``) und Request-IDs.

Umgebungsvariablen:

* ``LOG_LEVEL`` – Level des Root-Loggers (``DEBUG``, ``INFO``, ``WARNING``,
  ``ERROR``, ``CRITICAL``; Default ``INFO``).
* ``LOG_FORMAT`` – ``text`` (Default, eine lesbare Zeile) oder ``json`` (eine
  JSON-Zeile je Eintrag, z. B. für Loki/ELK).

Jeder Log-Eintrag trägt die Request-ID des aktuellen HTTP-Requests (``-``
außerhalb eines Requests). Die ID kommt aus dem Header ``X-Request-ID`` des
Aufrufers (z. B. vom Reverse-Proxy gesetzt) oder wird erzeugt; die Antwort
trägt sie im selben Header, sodass Client-Fehler und Server-Logs zuzuordnen sind.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from logging.config import dictConfig
from typing import Any

from flask import Flask, g, has_request_context, request

REQUEST_ID_HEADER = "X-Request-ID"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "text"
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
VALID_FORMATS = ("text", "json")
TEXT_FORMAT = "%(asctime)s %(levelname)-5.5s [%(name)s] [req=%(request_id)s] %(message)s"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def current_request_id() -> str | None:
    """Request-ID des aktiven Requests oder ``None`` außerhalb eines Request-Kontexts."""
    if not has_request_context():
        return None
    return getattr(g, "request_id", None)


class RequestIdFilter(logging.Filter):
    """Hängt ``request_id`` an jeden LogRecord (``-`` ohne Request-Kontext)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Eine JSON-Zeile je Eintrag (UTC-Zeitstempel, Level, Logger, Nachricht, Request-ID)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def normalize_level(value: str | None) -> str:
    level = (value or DEFAULT_LOG_LEVEL).strip().upper()
    return level if level in VALID_LEVELS else DEFAULT_LOG_LEVEL


def normalize_format(value: str | None) -> str:
    fmt = (value or DEFAULT_LOG_FORMAT).strip().lower()
    return fmt if fmt in VALID_FORMATS else DEFAULT_LOG_FORMAT


def build_logging_config(level: str | None = None, fmt: str | None = None) -> dict[str, Any]:
    """dictConfig-Struktur: ein Konsolen-Handler (stderr) am Root-Logger.

    ``disable_existing_loggers`` bleibt aus, damit bereits erzeugte Modul-Logger
    (``app.*``, ``alembic``, ``werkzeug``) weiter durchgereicht werden.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_id": {"()": "app.logging_config.RequestIdFilter"}},
        "formatters": {
            "text": {"format": TEXT_FORMAT},
            "json": {"()": "app.logging_config.JsonFormatter"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": normalize_format(fmt),
                "filters": ["request_id"],
            }
        },
        "root": {"level": normalize_level(level), "handlers": ["console"]},
    }


def configure_logging(app: Flask) -> None:
    """Konfiguriert das Prozess-Logging aus ``LOG_LEVEL``/``LOG_FORMAT``.

    Unter ``TESTING`` wird das globale Logging nicht angefasst (pytest verwaltet
    seine eigenen Handler); die aufgelösten Werte stehen trotzdem in ``app.config``.
    """
    level = normalize_level(app.config.get("LOG_LEVEL") or os.environ.get("LOG_LEVEL"))
    fmt = normalize_format(app.config.get("LOG_FORMAT") or os.environ.get("LOG_FORMAT"))
    app.config["LOG_LEVEL"] = level
    app.config["LOG_FORMAT"] = fmt
    if app.config.get("TESTING"):
        return
    dictConfig(build_logging_config(level, fmt))


def init_request_id(app: Flask) -> None:
    """Request-ID je Request vergeben (Header übernehmen oder erzeugen) und zurückgeben."""

    @app.before_request
    def _assign_request_id() -> None:
        incoming = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
        g.request_id = incoming if _REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex

    @app.after_request
    def _echo_request_id(response):
        request_id = current_request_id()
        if request_id:
            response.headers.setdefault(REQUEST_ID_HEADER, request_id)
        return response
