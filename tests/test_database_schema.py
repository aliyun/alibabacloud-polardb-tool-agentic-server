from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.core.config_crypto import ConfigCrypto


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.parametrize("entrypoint", ["check_database_schema", "inspect_database", "run_migration"])
async def test_cancelled_database_operation_releases_cursor_read_lock(tmp_path, monkeypatch, entrypoint):
    from sqlalchemy import event
    from server.db import migration_runner, schema

    module = migration_runner if entrypoint == "run_migration" else schema

    path = tmp_path / "cancelled-schema.db"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        setup.execute("INSERT INTO alembic_version VALUES (?)", (schema.required_schema_head(),))
        setup.execute("CREATE TABLE committed_values (value INTEGER)")
        setup.execute("INSERT INTO committed_values VALUES (1)")

    entered = threading.Event()
    release = threading.Event()
    native_connections = []
    real_create_engine = module.create_async_engine

    def instrument_engine(*args, **kwargs):
        engine = real_create_engine(*args, **kwargs)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def pause_schema_select(connection, _cursor, statement, _params, _context, _many):
            if not statement.startswith("SELECT alembic_version.version_num"):
                return
            dbapi = connection.connection.dbapi_connection
            native_connections.append(dbapi.driver_connection._conn)
            steps = 0

            def pause():
                nonlocal steps
                steps += 1
                if steps == 4:
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("schema query was not released")
                return 0

            dbapi.run_async(lambda driver: driver.set_progress_handler(pause, 1))

        return engine

    monkeypatch.setattr(module, "create_async_engine", instrument_engine)
    task = asyncio.create_task(getattr(module, entrypoint)(_database_url(path)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as retained_error:
            await task
        assert retained_error.value.__traceback__ is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            native_connections[0].execute("SELECT 1")
        with sqlite3.connect(path, timeout=0.02) as writer:
            writer.execute("INSERT INTO committed_values VALUES (2)")
            writer.commit()
            assert writer.execute("SELECT value FROM committed_values ORDER BY value").fetchall() == [(1,), (2,)]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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


def test_visibility_exclusion_migration_preserves_explicit_selections(tmp_path: Path) -> None:
    from server.db.schema import check_database_schema, migrate_database, required_schema_head

    database = tmp_path / "kb-exclusions.db"
    database_url = _database_url(database)
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "9c1d2e3f4a5b")

    async def seed_parents():
        from server.models import AuthProvider, PolarRAGInstance, User

        engine = create_async_engine(database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                user = User(external_id="migration-admin", display_name="Admin", auth_provider=AuthProvider.BUILTIN)
                session.add(user)
                await session.flush()
                agent_id = "migration-agent"
                await session.execute(text(
                    "INSERT INTO agents (id, name, status, created_at) "
                    "VALUES (:id, 'Migration', 'ACTIVE', '2026-09-16 00:00:00')"
                ), {"id": agent_id})
                instance = PolarRAGInstance(
                    name="Migration", scheme="http", host="rag.example.test", port=9200,
                    username_ciphertext="username", password_ciphertext="password", created_by=user.id,
                )
                session.add(instance)
                await session.commit()
                return agent_id, instance.id
        finally:
            await engine.dispose()

    agent_id, instance_id = asyncio.run(seed_parents())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agent_polarrag_instance_bindings "
            "(id, agent_id, polarrag_instance_id, public_knowledge_resource_ids_json, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("binding-sentinel", agent_id, instance_id, '["existing"]',
             "2026-09-16 00:00:00"),
        )
    migrate_database(database_url, maintenance=True)
    migrate_database(database_url, maintenance=True)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT public_knowledge_resource_ids_json, personal_knowledge_resource_ids_json, "
            "public_excluded_resource_ids_json, personal_excluded_resource_ids_json "
            "FROM agent_polarrag_instance_bindings WHERE id = 'binding-sentinel'"
        ).fetchone()
    assert row == ('["existing"]', None, None, None)
    assert asyncio.run(check_database_schema(database_url)) == required_schema_head()


def test_dynamic_builtin_grant_upgrade_preserves_existing_agent_scope(tmp_path: Path) -> None:
    from server.db.schema import check_database_schema, migrate_database, required_schema_head

    database = tmp_path / "builtin-users.db"
    database_url = _database_url(database)
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "be3f4a5b6c7d")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agents (id, name, status, created_at) "
            "VALUES ('old-agent', 'Old', 'ACTIVE', '2026-09-17 00:00:00')"
        )
    migrate_database(database_url, maintenance=True)
    migrate_database(database_url, maintenance=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT all_builtin_users FROM agents WHERE id='old-agent'").fetchone() == (0,)
    assert asyncio.run(check_database_schema(database_url)) == required_schema_head()


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
    from server.db.schema import (
        check_database_schema,
        migrate_database,
        required_schema_head,
    )

    database = tmp_path / "current.db"
    await asyncio.to_thread(migrate_database, _database_url(database), maintenance=True)

    assert await check_database_schema(_database_url(database)) == (
        required_schema_head()
    )


async def test_check_database_schema_rejects_same_head_physical_drift(
    tmp_path: Path,
) -> None:
    from server.db.schema import DatabaseSchemaError, check_database_schema

    database = tmp_path / "drifted.db"
    _stamp(database, "f6a7b8c9d0e1")

    with pytest.raises(DatabaseSchemaError) as captured:
        await check_database_schema(_database_url(database))

    assert captured.value.code == "DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN"


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
    migrate_database(database_url, maintenance=True)

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

    migrate_database(database_url, maintenance=True)

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


def test_migrate_database_repairs_prerelease_knowledge_scope_table_name(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        check_database_schema,
        migrate_database,
        required_schema_head,
    )

    database = tmp_path / "prerelease-knowledge-scopes.db"
    database_url = _database_url(database)
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "9c1d2e3f4a5b")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO enterprise_identity_sources "
            "(id, name, provider, tenant_id, status, stale_after_seconds, "
            "sync_retry_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "source-prerelease",
                "Source prerelease",
                "feishu",
                "tenant-prerelease",
                "active",
                1800,
                0,
                "2026-09-14 00:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO enterprise_directory_memberships "
            "(id, identity_source_id, external_group_id, member_type, "
            "external_member_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "membership-prerelease",
                "source-prerelease",
                "group-prerelease",
                "user",
                "user-prerelease",
                "2026-09-14 00:00:00",
                "2026-09-14 00:00:00",
            ),
        )
        connection.execute(
            "ALTER TABLE enterprise_directory_memberships "
            "RENAME TO identity_source_group_memberships"
        )
        connection.execute(
            "ALTER TABLE identity_source_user_principal_snapshot_entries "
            "ADD COLUMN source_subject_type VARCHAR(32) NOT NULL "
            "DEFAULT 'user'"
        )

    migrate_database(database_url, maintenance=True)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "enterprise_directory_memberships" in tables
        assert "identity_source_group_memberships" not in tables
        assert connection.execute(
            "SELECT id, external_group_id, external_member_id "
            "FROM enterprise_directory_memberships"
        ).fetchall() == [
            ("membership-prerelease", "group-prerelease", "user-prerelease")
        ]
        assert "source_subject_type" in {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(identity_source_user_principal_snapshot_entries)"
            )
        }
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (required_schema_head(),)
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
    migrate_database(database_url, maintenance=True)
    asyncio.run(_initialize_configuration(database_url, root_key))

    assert asyncio.run(
        check_database_compatibility(database_url, root_key)
    ) == required_schema_head()


def _downgrade_audit_modules_to_v1(
    database: Path,
    *,
    audit_enabled: object = False,
    audit_retention_days: object = 45,
) -> None:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT config_key, config_value FROM system_config "
            "WHERE config_key IN (?, ?)",
            ("module.sql_security", "module.observability"),
        ).fetchall()
        assert len(rows) == 2
        documents = {key: json.loads(value) for key, value in rows}
        sql = documents["module.sql_security"]
        observability = documents["module.observability"]
        sql["schema_version"] = 1
        sql["effective"]["config"].update(
            {
                "audit_enabled": audit_enabled,
                "audit_retention_days": audit_retention_days,
            }
        )
        observability["schema_version"] = 1
        observability["effective"]["config"].pop("audit_enabled")
        observability["effective"]["config"].pop(
            "audit_retention_days"
        )
        connection.executemany(
            "UPDATE system_config SET config_value = ? "
            "WHERE config_key = ?",
            (
                (
                    json.dumps(sql, separators=(",", ":")),
                    "module.sql_security",
                ),
                (
                    json.dumps(observability, separators=(",", ":")),
                    "module.observability",
                ),
            ),
        )


def test_database_compatibility_projects_legacy_audit_without_writing(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        check_database_compatibility,
        migrate_database,
        required_schema_head,
    )

    root_key = b"d" * 32
    database = tmp_path / "legacy-audit.db"
    database_url = _database_url(database)
    migrate_database(database_url, maintenance=True)
    asyncio.run(_initialize_configuration(database_url, root_key))
    _downgrade_audit_modules_to_v1(database)

    assert asyncio.run(
        check_database_compatibility(database_url, root_key)
    ) == required_schema_head()

    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT json_extract(config_value, '$.schema_version') "
            "FROM system_config WHERE config_key IN (?, ?) "
            "ORDER BY config_key",
            ("module.sql_security", "module.observability"),
        ).fetchall()
    assert versions == [(1,), (1,)]


def test_database_compatibility_rejects_invalid_legacy_audit(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        DatabaseSchemaError,
        check_database_compatibility,
        migrate_database,
    )

    root_key = b"e" * 32
    database = tmp_path / "invalid-legacy-audit.db"
    database_url = _database_url(database)
    migrate_database(database_url, maintenance=True)
    asyncio.run(_initialize_configuration(database_url, root_key))
    _downgrade_audit_modules_to_v1(
        database,
        audit_retention_days="invalid",
    )

    with pytest.raises(DatabaseSchemaError) as captured:
        asyncio.run(
            check_database_compatibility(database_url, root_key)
        )

    assert captured.value.code == "DATABASE_CONFIGURATION_INCOMPATIBLE"
    assert captured.value.__cause__ is None


def test_database_compatibility_rejects_mismatched_root_key(
    tmp_path: Path,
) -> None:
    from server.db.schema import (
        DatabaseSchemaError,
        check_database_compatibility,
        migrate_database,
    )

    database_url = _database_url(tmp_path / "mismatched.db")
    migrate_database(database_url, maintenance=True)
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
    migrate_database(database_url, maintenance=True)

    assert asyncio.run(
        check_database_compatibility(database_url, b"c" * 32)
    ) == required_schema_head()


@pytest.mark.parametrize("definition,index_sql", [
    ("VARCHAR(35)", None),
    ("VARCHAR(36) NOT NULL", None),
    ("VARCHAR(36) DEFAULT ''", None),
    ("VARCHAR(36)", "CREATE UNIQUE INDEX ix_knowledge_resources_catalog_sync_token ON knowledge_resources(catalog_sync_token)"),
])
def test_migrate_rejects_incompatible_preexisting_deployment_columns(tmp_path: Path, definition, index_sql) -> None:
    url = _database_url(tmp_path / "incompatible-deployment.db")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "c3d4e5f6a7b8")
    with sqlite3.connect(tmp_path / "incompatible-deployment.db") as connection:
        connection.execute(f"ALTER TABLE knowledge_resources ADD COLUMN catalog_sync_token {definition}")
        if index_sql is not None:
            connection.execute(index_sql)
    with pytest.raises(RuntimeError, match="Incompatible existing"):
        command.upgrade(config, "fb1c2d3e4f5a")


def test_migrate_preserves_preexisting_deployment_columns(tmp_path: Path) -> None:
    database = tmp_path / "deployment.db"
    url = _database_url(database)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "c3d4e5f6a7b8")
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE knowledge_resources ADD COLUMN catalog_sync_token VARCHAR(36)")
        connection.execute("ALTER TABLE agents ADD COLUMN oauth_redirect_uri TEXT")
        connection.execute("CREATE INDEX ix_knowledge_resources_catalog_sync_token ON knowledge_resources(catalog_sync_token)")
        connection.execute(
            "INSERT INTO agents (id,name,status,created_at,oauth_redirect_uri) VALUES (?,?,?,?,?)",
            ("legacy-agent", "legacy", "ACTIVE", "2026-09-01", "https://example.com/callback"),
        )
    from server.db.schema import migrate_database, check_database_schema, required_schema_head

    migrate_database(url, maintenance=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT oauth_redirect_uri FROM agents WHERE id='legacy-agent'").fetchone() == ("https://example.com/callback",)
    assert asyncio.run(check_database_schema(url)) == required_schema_head()
