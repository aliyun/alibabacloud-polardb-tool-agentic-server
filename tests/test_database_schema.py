from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.core.config_crypto import ConfigCrypto


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _stamp(path: Path, revision: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (revision,),
        )


async def _initialize_configuration(
    database_url: str, root_key: bytes
) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await initialize_configuration(
            ConfigRepository(factory), ConfigCrypto(root_key)
        )
    finally:
        await engine.dispose()


def test_required_schema_head_rejects_multiple_heads(monkeypatch) -> None:
    from server.db import schema

    class MultipleHeads:
        @staticmethod
        def get_heads() -> list[str]:
            return ["head-a", "head-b"]

    monkeypatch.setattr(
        schema.ScriptDirectory,
        "from_config",
        lambda _config: MultipleHeads(),
    )

    with pytest.raises(schema.DatabaseSchemaError) as captured:
        schema.required_schema_head()

    assert captured.value.code == "DATABASE_MIGRATION_HEAD_INVALID"


def test_alembic_config_uses_installed_package_migrations(
    monkeypatch, tmp_path: Path
) -> None:
    from server.db import schema

    monkeypatch.setattr(schema, "_ROOT", tmp_path)

    script_location = Path(
        schema._alembic_config().get_main_option("script_location")
    )

    assert schema._alembic_config().config_file_name is None
    assert script_location.is_absolute()
    assert (script_location / "env.py").is_file()
    assert (script_location / "versions").is_dir()


async def test_check_database_schema_rejects_uninitialized_database(
    tmp_path: Path,
) -> None:
    from server.db.schema import DatabaseSchemaError, check_database_schema

    with pytest.raises(DatabaseSchemaError) as captured:
        await check_database_schema(_database_url(tmp_path / "empty.db"))

    assert captured.value.code == "DATABASE_SCHEMA_NOT_INITIALIZED"


async def test_check_database_schema_accepts_current_revision(
    tmp_path: Path,
) -> None:
    from server.db.schema import check_database_schema, required_schema_head

    database = tmp_path / "current.db"
    _stamp(database, required_schema_head())

    assert await check_database_schema(_database_url(database)) == (
        required_schema_head()
    )


async def test_check_database_schema_rejects_known_older_revision(
    tmp_path: Path,
) -> None:
    from server.db.schema import DatabaseSchemaError, check_database_schema

    database = tmp_path / "old.db"
    _stamp(database, "c0f1a2b3c4d5")

    with pytest.raises(DatabaseSchemaError) as captured:
        await check_database_schema(_database_url(database))

    assert captured.value.code == "DATABASE_SCHEMA_OUTDATED"


async def test_check_database_schema_rejects_unknown_newer_revision(
    tmp_path: Path,
) -> None:
    from server.db.schema import DatabaseSchemaError, check_database_schema

    database = tmp_path / "new.db"
    _stamp(database, "ffffffffffff")

    with pytest.raises(DatabaseSchemaError) as captured:
        await check_database_schema(_database_url(database))

    assert captured.value.code == "DATABASE_SCHEMA_TOO_NEW"


async def test_check_database_schema_sanitizes_unavailable_url() -> None:
    from server.db.schema import DatabaseSchemaError, check_database_schema

    database_url = (
        "mysql+asyncmy://pas_user:do-not-print@127.0.0.1:1/pas"
    )
    with pytest.raises(DatabaseSchemaError) as captured:
        await check_database_schema(database_url)

    assert captured.value.code == "DATABASE_UNAVAILABLE"
    assert "do-not-print" not in str(captured.value)
    assert database_url not in str(captured.value)
    assert captured.value.__cause__ is None


def test_migrate_database_initializes_fresh_sqlite(tmp_path: Path) -> None:
    from server.db.schema import (
        check_database_schema,
        migrate_database,
        required_schema_head,
    )

    database_url = _database_url(tmp_path / "migrated.db")
    migrate_database(database_url)

    assert asyncio.run(check_database_schema(database_url)) == (
        required_schema_head()
    )


def test_migrate_database_preserves_populated_pre_head_data(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        check_database_schema,
        migrate_database,
        required_schema_head,
    )

    database = tmp_path / "upgrade.db"
    database_url = _database_url(database)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "c0f1a2b3c4d5")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO system_config("
            "config_key, config_value, config_version, created_at"
            ") VALUES (?, ?, ?, ?)",
            ("test.sentinel", '{"preserved":true}', 7, "2026-07-27"),
        )

    migrate_database(database_url)

    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT config_value, config_version "
            "FROM system_config WHERE config_key = ?",
            ("test.sentinel",),
        ).fetchone()
    assert stored == ('{"preserved":true}', 7)
    assert asyncio.run(check_database_schema(database_url)) == (
        required_schema_head()
    )


def test_database_compatibility_accepts_matching_root_key(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        check_database_compatibility,
        migrate_database,
        required_schema_head,
    )

    root_key = b"a" * 32
    database_url = _database_url(tmp_path / "compatible.db")
    migrate_database(database_url)
    asyncio.run(_initialize_configuration(database_url, root_key))

    assert asyncio.run(
        check_database_compatibility(database_url, root_key)
    ) == required_schema_head()


def test_database_compatibility_rejects_mismatched_root_key(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        DatabaseSchemaError,
        check_database_compatibility,
        migrate_database,
    )

    database_url = _database_url(tmp_path / "mismatched.db")
    migrate_database(database_url)
    asyncio.run(_initialize_configuration(database_url, b"a" * 32))

    with pytest.raises(DatabaseSchemaError) as captured:
        asyncio.run(
            check_database_compatibility(database_url, b"b" * 32)
        )

    assert captured.value.code == "DATABASE_ENCRYPTION_KEY_MISMATCH"
    assert "b" * 32 not in str(captured.value)
    assert captured.value.__cause__ is None


def test_database_compatibility_accepts_migrated_uninitialized_database(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        check_database_compatibility,
        migrate_database,
        required_schema_head,
    )

    database_url = _database_url(tmp_path / "uninitialized.db")
    migrate_database(database_url)

    assert asyncio.run(
        check_database_compatibility(database_url, b"c" * 32)
    ) == required_schema_head()
