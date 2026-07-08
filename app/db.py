from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from domain.models import Base

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_sqlite_path() -> str:
    instance_dir = PROJECT_ROOT / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{instance_dir / 'openbuchhaltung.db'}"


def _alembic_config(engine) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return config


def _alembic_head_revision() -> str | None:
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # pragma: no cover - defensiv, z. B. ohne migrations/-Verzeichnis
        logger.warning("Alembic-Head konnte nicht ermittelt werden.", exc_info=True)
        return None


def _current_revision(engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _bootstrap_schema(engine) -> None:
    """Bringt die Datenbank beim Start auf den aktuellen Stand.

    * **Leere DB:** Schema per ``create_all`` anlegen und auf den Alembic-Head stampen.
    * **Von der App verwaltete DB** (besitzt ``alembic_version``): ausstehende
      Migrationen automatisch via ``alembic upgrade head`` nachziehen — damit ein
      Redeploy gegen eine bestehende Datenbank neue Migrationen selbst anwendet.
    * **Bestehende DB ohne ``alembic_version``:** unberührt lassen (extern verwaltet);
      ein ``create_all``/Upgrade würde an Alembic vorbei laufen bzw. bestehende
      Tabellen kollidieren lassen.
    """
    tables = inspect(engine).get_table_names()

    if not tables:
        Base.metadata.create_all(engine)
        head = _alembic_head_revision()
        if head is None:
            return
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:head)"), {"head": head}
            )
        logger.info("Neues Schema angelegt und auf Alembic-Revision %s gestampt.", head)
        return

    if "alembic_version" not in tables:
        # Extern verwaltete DB — nicht anfassen.
        return

    _upgrade_to_head(engine)


def _upgrade_to_head(engine) -> None:
    """Wendet ausstehende Migrationen auf eine verwaltete DB an (Fail-fast bei Fehler)."""
    head = _alembic_head_revision()
    if head is None:
        return
    current = _current_revision(engine)
    if current == head:
        return
    try:
        command.upgrade(_alembic_config(engine), "head")
    except Exception:
        logger.exception(
            "Automatische DB-Migration von %s auf %s fehlgeschlagen.", current, head
        )
        raise
    logger.info("Datenbank von Revision %s auf Head %s migriert.", current, head)


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = create_engine(database_url or _resolve_sqlite_path(), future=True)
    _bootstrap_schema(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
