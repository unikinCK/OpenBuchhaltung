from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from app.db import create_session_factory


def _alembic_version(db_path: Path) -> str | None:
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
        ).first()
        if not exists:
            return None
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_empty_database_is_created_and_stamped(tmp_path: Path):
    db_path = tmp_path / "fresh.db"
    create_session_factory(f"sqlite+pysqlite:///{db_path}")

    # Schema wurde angelegt ...
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    tables = set(engine.dialect.get_table_names(engine.connect()))
    assert {"tenant", "company", "account", "journal_entry"} <= tables

    # ... und auf eine konkrete Alembic-Revision gestampt (nicht None/leer).
    version = _alembic_version(db_path)
    assert version is not None and version != ""


def test_existing_database_is_not_recreated(tmp_path: Path):
    db_path = tmp_path / "existing.db"

    # DB simulieren, die bereits Tabellen hat, aber noch keinen alembic_version-Eintrag.
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tenant (id INTEGER PRIMARY KEY, name TEXT)"))

    create_session_factory(f"sqlite+pysqlite:///{db_path}")

    # Bestehende DB wird nicht angefasst: kein create_all, kein Stamp.
    assert _alembic_version(db_path) is None
    with engine.connect() as connection:
        columns = [row[1] for row in connection.execute(text("PRAGMA table_info(tenant)"))]
    assert columns == ["id", "name"]
