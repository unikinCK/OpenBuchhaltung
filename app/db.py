from __future__ import annotations

import logging
from pathlib import Path

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


def _alembic_head_revision() -> str | None:
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # pragma: no cover - defensiv, z. B. ohne migrations/-Verzeichnis
        logger.warning("Alembic-Head konnte nicht ermittelt werden.", exc_info=True)
        return None


def _bootstrap_schema(engine) -> None:
    """Legt das Schema nur für leere Datenbanken an und stampt sie auf den Alembic-Head.

    Bestehende Datenbanken werden ausschließlich über `alembic upgrade head`
    verwaltet — ein create_all würde neue Tabellen an Alembic vorbei anlegen
    und nachfolgende Migrationen brechen lassen.
    """
    if inspect(engine).get_table_names():
        return

    Base.metadata.create_all(engine)

    head = _alembic_head_revision()
    if head is None:
        return
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"), {"head": head}
        )
    logger.info("Neues Schema angelegt und auf Alembic-Revision %s gestampt.", head)


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = create_engine(database_url or _resolve_sqlite_path(), future=True)
    _bootstrap_schema(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
