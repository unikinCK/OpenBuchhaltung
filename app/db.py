from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

AUTO_MIGRATE_ENV = "DB_AUTO_MIGRATE"
_FALSE_VALUES = {"0", "false", "no", "off"}
MIGRATE_HINT = (
    "Bitte `alembic upgrade head` ausführen (Produktion: "
    "`docker compose -f docker-compose.production.yml run --rm app alembic upgrade head`)."
)


class SchemaNotReadyError(RuntimeError):
    """Datenbankschema passt nicht zum Code und darf nicht automatisch migriert werden."""


def auto_migrate_enabled() -> bool:
    """``DB_AUTO_MIGRATE`` (Default an). ``0``/``false``/``no``/``off`` schaltet ab."""
    return os.environ.get(AUTO_MIGRATE_ENV, "1").strip().lower() not in _FALSE_VALUES


def running_from_cli() -> bool:
    """True, wenn die App über das ``flask``-Kommando geladen wurde (Flask setzt das Flag)."""
    return os.environ.get("FLASK_RUN_FROM_CLI") == "true"


def _resolve_sqlite_path() -> str:
    instance_dir = PROJECT_ROOT / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{instance_dir / 'openbuchhaltung.db'}"


def _script_config() -> Config:
    # Bewusst ohne alembic.ini: deren ``fileConfig``-Logging würde beim In-Prozess-Upgrade
    # die Logging-Konfiguration der App überschreiben (siehe migrations/env.py).
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def _alembic_config(engine) -> Config:
    config = _script_config()
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return config


@lru_cache(maxsize=1)
def _alembic_head_revision() -> str | None:
    try:
        return ScriptDirectory.from_config(_script_config()).get_current_head()
    except Exception:  # pragma: no cover - defensiv, z. B. ohne migrations/-Verzeichnis
        logger.warning("Alembic-Head konnte nicht ermittelt werden.", exc_info=True)
        return None


def _current_revision(engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _bootstrap_schema(engine, *, auto_migrate: bool = True, cli_context: bool = False) -> None:
    """Bringt die Datenbank beim Start auf den aktuellen Stand bzw. prüft ihn.

    * **Leere DB:** alle Migrationen bis zum Alembic-Head anwenden. Dadurch werden
      neben Tabellen auch DB-seitige Schutzmechanismen wie Trigger installiert.
    * **Von der App verwaltete DB** (besitzt ``alembic_version``): ausstehende
      Migrationen automatisch via ``alembic upgrade head`` nachziehen — damit ein
      Redeploy gegen eine bestehende Datenbank neue Migrationen selbst anwendet.
    * **Bestehende DB ohne ``alembic_version``:** unberührt lassen (extern verwaltet);
      ein ``create_all``/Upgrade würde an Alembic vorbei laufen bzw. bestehende
      Tabellen kollidieren lassen.

    ``auto_migrate=False`` (``DB_AUTO_MIGRATE=0``, Produktion): Die App fasst das
    Schema nie an. Migrationen laufen als eigener Schritt vor dem Start
    (``alembic upgrade head``); ist die DB leer oder im Rückstand, bricht der Start
    fail-fast mit :class:`SchemaNotReadyError` ab, statt mit Schema-Fehlern zu laufen.

    ``cli_context=True`` (``flask``-Kommandos wie ``verify-integrity``): Eine leere
    DB wird angelegt (damit z. B. ``seed-demo`` funktioniert), eine bestehende DB
    im Rückstand aber nicht nebenbei migriert — Migrationen sind ein bewusster
    Schritt und kein Seiteneffekt eines Wartungskommandos.
    """
    tables = inspect(engine).get_table_names()

    if not tables:
        if not auto_migrate:
            raise SchemaNotReadyError(
                f"Datenbank ist leer und {AUTO_MIGRATE_ENV}=0 verhindert die "
                f"automatische Initialisierung. {MIGRATE_HINT}"
            )
        command.upgrade(_alembic_config(engine), "head")
        logger.info("Neues Schema vollständig bis zum Alembic-Head migriert.")
        return

    if "alembic_version" not in tables:
        # Extern verwaltete DB — nicht anfassen.
        return

    head = _alembic_head_revision()
    if head is None:
        return
    current = _current_revision(engine)
    if current == head:
        return

    if not auto_migrate:
        raise SchemaNotReadyError(
            f"Datenbank steht auf Revision {current}, der Code erwartet Head {head}; "
            f"automatische Migration ist per {AUTO_MIGRATE_ENV}=0 deaktiviert. {MIGRATE_HINT}"
        )
    if cli_context:
        logger.warning(
            "Datenbank steht auf Revision %s (Head %s). CLI-Aufrufe migrieren nicht "
            "automatisch; Migration per App-Start oder `alembic upgrade head`.",
            current,
            head,
        )
        return

    _upgrade_to_head(engine, current=current, head=head)


def _upgrade_to_head(engine, *, current: str | None, head: str) -> None:
    """Wendet ausstehende Migrationen auf eine verwaltete DB an (Fail-fast bei Fehler)."""
    try:
        command.upgrade(_alembic_config(engine), "head")
    except Exception:
        logger.exception("Automatische DB-Migration von %s auf %s fehlgeschlagen.", current, head)
        raise
    logger.info("Datenbank von Revision %s auf Head %s migriert.", current, head)


def _configure_sqlite(engine) -> None:
    """SQLite: Fremdschlüssel erzwingen und Datei-DBs im WAL-Modus betreiben.

    SQLite prüft Fremdschlüssel nur mit ``PRAGMA foreign_keys=ON`` je Verbindung;
    ohne das Pragma blieben verwaiste Zeilen unbemerkt. WAL erlaubt gleichzeitige
    Leser während eines Schreibers (In-Memory-DBs unterstützen kein WAL).
    """
    if engine.dialect.name != "sqlite":
        return
    is_file_database = engine.url.database not in (None, "", ":memory:")

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            if is_file_database:
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


def create_session_factory(
    database_url: str | None = None,
    *,
    auto_migrate: bool | None = None,
    cli_context: bool | None = None,
) -> sessionmaker[Session]:
    """Engine + Session-Factory; prüft bzw. migriert das Schema (siehe ``_bootstrap_schema``).

    ``pool_pre_ping`` verwirft tote Verbindungen (z. B. nach einem DB-Neustart),
    statt den ersten Request danach mit einem Verbindungsfehler scheitern zu lassen.
    """
    engine = create_engine(database_url or _resolve_sqlite_path(), future=True, pool_pre_ping=True)
    _configure_sqlite(engine)
    _bootstrap_schema(
        engine,
        auto_migrate=auto_migrate_enabled() if auto_migrate is None else auto_migrate,
        cli_context=running_from_cli() if cli_context is None else cli_context,
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def database_health(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    """Verbindungs- und Schemastatus für den Health-Endpoint.

    Rückgabe: ``{"database": {"ok", "dialect"[, "error"]},
    "schema": {"revision", "head", "up_to_date"}}``. ``up_to_date`` ist ``None``,
    wenn die DB nicht von Alembic verwaltet wird (kein ``alembic_version``) oder
    nicht erreichbar ist.
    """
    engine = session_factory.kw["bind"]
    database: dict[str, Any] = {"ok": False, "dialect": engine.dialect.name}
    schema: dict[str, Any] = {
        "revision": None,
        "head": _alembic_head_revision(),
        "up_to_date": None,
    }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            database["ok"] = True
            if inspect(connection).has_table("alembic_version"):
                schema["revision"] = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                schema["up_to_date"] = schema["revision"] == schema["head"]
    except Exception as exc:
        database["error"] = type(exc).__name__
        logger.warning("Health-Check: Datenbank nicht erreichbar (%s).", type(exc).__name__)
    return {"database": database, "schema": schema}
