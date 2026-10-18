"""Betriebs-Härtung (Review-Sprint 3): Health-Endpoint, Migrationsmodus,
SQLite-Pragmas, Logging/Request-ID, Version und ENV-Referenz."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import create_app
from app.db import (
    SchemaNotReadyError,
    _alembic_config,
    _alembic_head_revision,
    create_session_factory,
    database_health,
)
from app.logging_config import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    RequestIdFilter,
    build_logging_config,
    normalize_format,
    normalize_level,
)
from app.version import get_commit, get_version
from domain.models import Company

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_REVISION = "20260706_0007"


def _url(tmp_path: Path, name: str) -> str:
    return f"sqlite+pysqlite:///{tmp_path / name}"


def _app(tmp_path: Path, **overrides):
    config = {"TESTING": True, "DATABASE_URL": _url(tmp_path, "ops.db")}
    config.update(overrides)
    return create_app(config)


def _revision(url: str) -> str | None:
    with create_engine(url).connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _outdated_database(url: str) -> None:
    command.upgrade(_alembic_config(create_engine(url)), OLD_REVISION)


# --- Health-Endpoint -----------------------------------------------------------


def test_health_reports_database_schema_version_and_request_id(tmp_path):
    app = _app(tmp_path)
    response = app.test_client().get("/api/v1/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["version"] == get_version()
    assert payload["database"] == {"ok": True, "dialect": "sqlite"}
    assert payload["schema"]["revision"] == _alembic_head_revision()
    assert payload["schema"]["up_to_date"] is True
    assert response.headers[REQUEST_ID_HEADER]


def test_health_returns_503_when_database_is_unreachable(tmp_path):
    app = _app(tmp_path)
    # Datei in nicht existierendem Verzeichnis: Verbindung schlägt fehl.
    app.extensions["db_session_factory"] = sessionmaker(
        bind=create_engine(_url(tmp_path / "fehlt", "x.db"))
    )

    response = app.test_client().get("/api/v1/health")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "unhealthy"
    assert payload["database"]["ok"] is False
    assert payload["database"]["error"] == "OperationalError"


def test_database_health_flags_schema_behind_head(tmp_path):
    url = _url(tmp_path, "behind.db")
    _outdated_database(url)

    result = database_health(sessionmaker(bind=create_engine(url)))

    assert result["database"]["ok"] is True
    assert result["schema"]["revision"] == OLD_REVISION
    assert result["schema"]["head"] == _alembic_head_revision()
    assert result["schema"]["up_to_date"] is False


def test_database_health_tolerates_externally_managed_schema(tmp_path):
    engine = create_engine(_url(tmp_path, "extern.db"))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenant (id INTEGER PRIMARY KEY)"))

    result = database_health(sessionmaker(bind=engine))

    assert result["database"]["ok"] is True
    assert result["schema"] == {
        "revision": None,
        "head": _alembic_head_revision(),
        "up_to_date": None,
    }


# --- Migrationsmodus (DB_AUTO_MIGRATE, CLI-Kontext) ------------------------------


def test_auto_migrate_disabled_refuses_empty_database(tmp_path):
    with pytest.raises(SchemaNotReadyError, match="DB_AUTO_MIGRATE=0"):
        create_session_factory(_url(tmp_path, "leer.db"), auto_migrate=False)


def test_auto_migrate_disabled_refuses_outdated_database(tmp_path):
    url = _url(tmp_path, "alt.db")
    _outdated_database(url)

    with pytest.raises(SchemaNotReadyError, match=OLD_REVISION):
        create_session_factory(url, auto_migrate=False)

    assert _revision(url) == OLD_REVISION  # nichts migriert


def test_auto_migrate_disabled_accepts_database_at_head(tmp_path):
    url = _url(tmp_path, "aktuell.db")
    create_session_factory(url)  # migriert bis Head

    factory = create_session_factory(url, auto_migrate=False)

    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1
    assert _revision(url) == _alembic_head_revision()


def test_cli_context_does_not_migrate_outdated_database(tmp_path):
    url = _url(tmp_path, "cli-alt.db")
    _outdated_database(url)

    create_session_factory(url, cli_context=True)

    assert _revision(url) == OLD_REVISION


def test_cli_context_initializes_empty_database(tmp_path):
    url = _url(tmp_path, "cli-leer.db")

    create_session_factory(url, cli_context=True)

    assert _revision(url) == _alembic_head_revision()


def test_create_app_reads_auto_migrate_switch_from_environment(tmp_path, monkeypatch):
    url = _url(tmp_path, "env.db")

    monkeypatch.setenv("DB_AUTO_MIGRATE", "0")
    with pytest.raises(SchemaNotReadyError):
        create_app({"TESTING": True, "DATABASE_URL": url})

    monkeypatch.setenv("DB_AUTO_MIGRATE", "1")
    app = create_app({"TESTING": True, "DATABASE_URL": url})
    assert app.config["DB_AUTO_MIGRATE"] is True
    assert _revision(url) == _alembic_head_revision()


def test_create_app_from_flask_cli_leaves_outdated_database_alone(tmp_path, monkeypatch):
    url = _url(tmp_path, "flask-cli.db")
    _outdated_database(url)
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")

    create_app({"TESTING": True, "DATABASE_URL": url})

    assert _revision(url) == OLD_REVISION


# --- SQLite-Pragmas und Pool ----------------------------------------------------


def test_sqlite_connections_enforce_foreign_keys_and_use_wal(tmp_path):
    factory = create_session_factory(_url(tmp_path, "pragma.db"))

    with factory() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        session.add(Company(name="Waise GmbH", currency_code="EUR", tenant_id=999_999))
        with pytest.raises(IntegrityError):
            session.flush()


def test_engine_uses_pool_pre_ping(tmp_path):
    factory = create_session_factory(_url(tmp_path, "pool.db"))

    assert factory.kw["bind"].pool._pre_ping is True


# --- Logging und Request-ID ---------------------------------------------------


def test_logging_config_structure_and_normalisation():
    config = build_logging_config("debug", "JSON")

    assert config["disable_existing_loggers"] is False
    assert config["root"] == {"level": "DEBUG", "handlers": ["console"]}
    assert config["handlers"]["console"]["formatter"] == "json"
    assert config["handlers"]["console"]["filters"] == ["request_id"]
    assert normalize_level("verbose") == "INFO"
    assert normalize_level(None) == "INFO"
    assert normalize_format("xml") == "text"


def test_json_formatter_emits_one_json_object_with_request_id():
    record = logging.LogRecord(
        "app.test", logging.WARNING, __file__, 1, "Hallo %s", ("Welt",), None
    )
    RequestIdFilter().filter(record)  # außerhalb eines Requests

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "Hallo Welt"
    assert payload["request_id"] == "-"
    assert payload["time"].endswith("+00:00")


def test_request_id_is_generated_or_taken_from_header(tmp_path):
    client = _app(tmp_path).test_client()

    generated = client.get("/api/v1/health").headers[REQUEST_ID_HEADER]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)

    echoed = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "proxy-abc.123"})
    assert echoed.headers[REQUEST_ID_HEADER] == "proxy-abc.123"

    rejected = client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "kein gueltiger wert"})
    assert re.fullmatch(r"[0-9a-f]{32}", rejected.headers[REQUEST_ID_HEADER])


def test_request_id_filter_reads_flask_request_context(tmp_path):
    app = _app(tmp_path)
    with app.test_request_context("/", headers={REQUEST_ID_HEADER: "req-1"}):
        app.preprocess_request()
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "x", (), None)
        RequestIdFilter().filter(record)

    assert record.request_id == "req-1"


def test_log_settings_are_resolved_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_FORMAT", "json")

    app = _app(tmp_path)

    assert app.config["LOG_LEVEL"] == "WARNING"
    assert app.config["LOG_FORMAT"] == "json"


# --- Version -------------------------------------------------------------------


def test_version_matches_pyproject_and_is_semver():
    version = get_version()

    assert re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.+-]+)?", version)
    assert f'version = "{version}"' in (PROJECT_ROOT / "pyproject.toml").read_text("utf-8")


def test_commit_prefers_environment_variable(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "abc123def456")
    get_commit.cache_clear()
    try:
        assert get_commit() == "abc123def456"
    finally:
        get_commit.cache_clear()


def test_create_app_exposes_version_and_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_COMMIT_SHA", "feedface")

    app = _app(tmp_path)

    assert app.config["APP_VERSION"] == get_version()
    assert app.config["APP_COMMIT_SHA"] == "feedface"


# --- ENV-Referenz: .env.example und README vollständig ---------------------------

_CODE_ENV_PATTERN = re.compile(
    r"(?:environ\.get|environ|getenv|_env_flag|_env_int)\(?\[?\s*\"([A-Z][A-Z0-9_]*)\""
)
_COMPOSE_ENV_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_INTERNAL_VARIABLES = {"FLASK_RUN_FROM_CLI"}  # von Flask selbst gesetzt


def _referenced_environment_variables() -> set[str]:
    names: set[str] = set()
    sources = [
        *PROJECT_ROOT.glob("app/**/*.py"),
        PROJECT_ROOT / "run.py",
        PROJECT_ROOT / "migrations" / "env.py",
    ]
    for path in sources:
        names.update(_CODE_ENV_PATTERN.findall(path.read_text(encoding="utf-8")))
    for path in PROJECT_ROOT.glob("docker-compose*.yml"):
        names.update(_COMPOSE_ENV_PATTERN.findall(path.read_text(encoding="utf-8")))
    return names - _INTERNAL_VARIABLES


def test_env_example_documents_every_referenced_variable():
    referenced = _referenced_environment_variables()
    assert len(referenced) > 30

    example = set(
        re.findall(
            r"^#?\s*([A-Z][A-Z0-9_]*)=",
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )

    assert sorted(referenced - example) == []


def test_readme_configuration_table_documents_every_referenced_variable():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    heading = "## Konfiguration (Umgebungsvariablen)"
    assert heading in readme
    section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]*)`", section))

    assert sorted(_referenced_environment_variables() - documented) == []
