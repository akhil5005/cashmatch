"""The Alembic migration must produce exactly the schema the models describe.

Without this test the two drift silently: tests pass against ``create_all``
while the deployed database, built by ``alembic upgrade head``, is subtly
different. Running the migration against a throwaway SQLite file keeps the
check fast and Docker-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Dialect

import cashmatch.models  # noqa: F401  (registers every table on Base.metadata)
from cashmatch.config import get_settings
from cashmatch.db.base import Base

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def migrated_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A SQLite database built by running the migrations, not by create_all."""
    url = f"sqlite+pysqlite:///{tmp_path / 'migrated.sqlite'}"
    # env.py reads the URL from Settings, so point Settings at the temp file.
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        get_settings.cache_clear()


def test_migration_creates_every_model_table(migrated_url: str) -> None:
    inspector = inspect(create_engine(migrated_url))
    migrated = set(inspector.get_table_names()) - {"alembic_version"}
    assert migrated == set(Base.metadata.tables)


def test_migrated_columns_match_the_models(migrated_url: str) -> None:
    """Same column names, same types, same nullability, table by table."""
    engine = create_engine(migrated_url)
    inspector = inspect(engine)
    dialect: Dialect = engine.dialect

    mismatches: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        migrated_columns = {c["name"]: c for c in inspector.get_columns(table_name)}

        missing = set(table.columns.keys()) - set(migrated_columns)
        extra = set(migrated_columns) - set(table.columns.keys())
        if missing or extra:
            mismatches.append(f"{table_name}: missing={sorted(missing)} extra={sorted(extra)}")
            continue

        for name, column in table.columns.items():
            migrated_column = migrated_columns[name]
            expected_type = column.type.compile(dialect)
            actual_type = str(migrated_column["type"])
            if expected_type != actual_type:
                mismatches.append(
                    f"{table_name}.{name}: model says {expected_type}, migration made {actual_type}"
                )
            if column.nullable != migrated_column["nullable"]:
                mismatches.append(
                    f"{table_name}.{name}: model nullable={column.nullable}, "
                    f"migration nullable={migrated_column['nullable']}"
                )

    assert not mismatches, "model/migration drift:\n  " + "\n  ".join(mismatches)


def test_migrated_indexes_match_the_models(migrated_url: str) -> None:
    inspector = inspect(create_engine(migrated_url))

    mismatches: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        expected = {index.name for index in table.indexes}
        # SQLite reports unique constraints as auto-indexes; ignore those.
        actual = {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index["name"].startswith("sqlite_")
        }
        if expected != actual:
            mismatches.append(
                f"{table_name}: model {sorted(expected)} vs migration {sorted(actual)}"
            )

    assert not mismatches, "index drift:\n  " + "\n  ".join(mismatches)


def test_downgrade_removes_everything(migrated_url: str) -> None:
    """A migration that cannot be undone is a migration you cannot deploy
    confidently."""
    command.downgrade(_alembic_config(migrated_url), "base")
    inspector = inspect(create_engine(migrated_url))
    assert set(inspector.get_table_names()) - {"alembic_version"} == set()
