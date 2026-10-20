from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from server.db import schema


@pytest.fixture
def metadata_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}"
    schema.migrate_database(url)
    return url


def _connect(url: str):
    return sqlite3.connect(url.removeprefix("sqlite+aiosqlite:///"))


def test_release_manifest_has_no_duplicate_keys():
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, f"Duplicate manifest key: {key}"
            result[key] = value
        return result

    manifest_path = Path(schema.__file__).with_name("schema_manifest.json")
    json.loads(manifest_path.read_text(), object_pairs_hook=unique_keys)


def test_contract_migration_publishes_new_generation_without_old_readers(metadata_url: str):
    from server.db import schema_contract

    with _connect(metadata_url) as connection:
        raw = connection.execute(
            "SELECT config_value FROM system_config WHERE config_key='schema.compatibility'"
        ).fetchone()
        assert raw is not None
        contract = json.loads(raw[0])
        assert contract["generation"] == 2
        assert contract["completed_head"] == schema.required_schema_head()
        assert contract["completed_sequence"] == 1
        assert contract["protocol_version"] == 1
        assert schema_contract.digest(schema_contract.manifest()) in contract["compatible_manifests"]
        assert (
            "814a368a1693949232057c975373f5af969e343f5278b7a0e4d18ba996eec456"
            not in contract["compatible_manifests"]
        )
        assert (
            "65f7f798736beb2ba768703e3eb6781794c40c555ed49e56e8d83d0d28633d9a"
            not in contract["compatible_manifests"]
        )
    before = asyncio.run(schema.inspect_database(metadata_url))
    schema.migrate_database(metadata_url)
    after = asyncio.run(schema.inspect_database(metadata_url))
    assert before["manifest_digest"] == after["manifest_digest"]
    assert after["compatible"] is True
    assert after["release_classification"] == "CONTRACT"


@pytest.mark.parametrize(
    "manifest,expected",
    [
        (
            {
                "baseline_head": "base",
                "head": "base",
                "release_classification": "NONE",
                "required_sequence": 0,
            },
            True,
        ),
        (
            {
                "baseline_head": "base",
                "head": "expand",
                "release_classification": "EXPAND",
                "required_sequence": 1,
            },
            True,
        ),
        (
            {
                "baseline_head": "base",
                "head": "expand",
                "release_classification": "NONE",
                "required_sequence": 1,
            },
            False,
        ),
        (
            {
                "baseline_head": "base",
                "head": "expand",
                "release_classification": "CONTRACT",
                "required_sequence": 1,
            },
            True,
        ),
    ],
)
def test_release_manifest_classification_is_fail_closed(manifest, expected):
    from server.db.migration_runner import _valid_release_manifest

    assert _valid_release_manifest(manifest, manifest["head"]) is expected


def test_manifest_mismatch_reports_actionable_preflight_failure(monkeypatch):
    from server.db import migration_runner, schema_contract

    password = "password-must-not-be-logged"
    manifest = {
        "baseline_head": "base",
        "head": "declared",
        "release_classification": "NONE",
        "required_sequence": 0,
    }
    reports: list[str] = []

    monkeypatch.setattr(schema_contract, "manifest", lambda: manifest)
    monkeypatch.setattr(schema, "required_schema_head", lambda: "actual")
    monkeypatch.setattr(
        migration_runner,
        "create_async_engine",
        lambda *args, **kwargs: pytest.fail(
            "manifest validation must happen before creating the database engine"
        ),
    )

    with pytest.raises(schema.DatabaseSchemaError) as error:
        asyncio.run(
            migration_runner.run_migration(
                f"mysql+asyncmy://user:{password}@db.example/pas",
                reporter=reports.append,
            )
        )

    assert error.value.code == "DATABASE_MIGRATION_MANIFEST_INVALID"
    rendered = "\n".join(reports)
    assert 'stage="manifest-validation"' in rendered
    assert 'status="failed"' in rendered
    assert 'required_head="actual"' in rendered
    assert 'manifest_head="declared"' in rendered
    assert 'release_classification="NONE"' in rendered
    assert "required_sequence=0" in rendered
    assert "database_connected=false" in rendered
    assert "schema_changes_attempted=false" in rendered
    assert "next_action=" in rendered
    assert password not in rendered
    assert "No database connection was opened" in str(error.value)
    assert password not in str(error.value)


def test_successful_migration_reports_safe_progress(tmp_path: Path):
    database_path = tmp_path / "reported-metadata.db"
    url = f"sqlite+aiosqlite:///{database_path}"
    reports: list[str] = []

    schema.migrate_database(url, reporter=reports.append, maintenance=True)

    rendered = "\n".join(reports)
    for stage in (
        "manifest-validation",
        "database-connect",
        "migration-lock",
        "schema-inspection",
        "schema-upgrade",
        "schema-validation",
        "complete",
    ):
        assert f'stage="{stage}"' in rendered
    assert 'stage="complete" status="succeeded"' in rendered
    assert 'result="target-schema-verified"' in rendered
    assert str(database_path) not in rendered
    assert url not in rendered


@pytest.mark.parametrize("failure_stage", ["repair-ddl", "after-repair", "no-repair"])
def test_prerelease_repair_failure_reports_schema_changes(
    tmp_path: Path, monkeypatch, failure_stage: str
):
    from alembic import command
    from sqlalchemy.engine import Connection

    from server.db import schema_contract

    url = f"sqlite+aiosqlite:///{tmp_path / 'prerelease.db'}"
    command.upgrade(schema._alembic_config(url), "9c1d2e3f4a5b")
    if failure_stage != "no-repair":
        with _connect(url) as connection:
            connection.execute(
                "ALTER TABLE enterprise_directory_memberships "
                "RENAME TO identity_source_group_memberships"
            )

    if failure_stage == "repair-ddl":
        execute = Connection.exec_driver_sql

        def fail_repair(connection, statement, *args, **kwargs):
            if statement.startswith("ALTER TABLE identity_source_group_memberships"):
                raise RuntimeError("injected repair DDL failure")
            return execute(connection, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "exec_driver_sql", fail_repair)
    else:
        def fail_inspection(connection):
            raise RuntimeError("injected schema inspection failure")

        monkeypatch.setattr(schema_contract, "physical_fingerprint", fail_inspection)

    reports: list[str] = []
    with pytest.raises(schema.DatabaseSchemaError) as error:
        schema.migrate_database(url, reporter=reports.append, maintenance=True)
    assert error.value.code == "DATABASE_MIGRATION_FAILED"
    failure = reports[-1]
    assert 'stage="schema-inspection" status="failed"' in failure
    attempted = "false" if failure_stage == "no-repair" else "true"
    assert f"schema_changes_attempted={attempted}" in failure
    with _connect(url) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert ("enterprise_directory_memberships" in tables) == (
        failure_stage != "repair-ddl"
    )


def test_same_head_missing_required_column_is_rejected(metadata_url: str):
    with _connect(metadata_url) as connection:
        connection.execute("ALTER TABLE agents DROP COLUMN oauth_redirect_uri")
    with pytest.raises(schema.DatabaseSchemaError) as error:
        asyncio.run(schema.check_database_schema(metadata_url))
    assert error.value.code == "DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN"


def test_declared_same_generation_expansion_accepts_source(metadata_url: str):
    with _connect(metadata_url) as connection:
        raw = connection.execute(
            "SELECT config_value FROM system_config WHERE config_key='schema.compatibility'"
        ).fetchone()[0]
        contract = json.loads(raw)
        contract["completed_head"] = "future_expand_1"
        contract["completed_sequence"] += 1
        connection.execute("CREATE TABLE future_optional_feature (id INTEGER PRIMARY KEY)")
        connection.execute("UPDATE alembic_version SET version_num='future_expand_1'")
        connection.execute(
            "UPDATE system_config SET config_value=? WHERE config_key='schema.compatibility'", (json.dumps(contract),)
        )
    assert asyncio.run(schema.check_database_schema(metadata_url)) == "future_expand_1"
    schema.migrate_database(metadata_url)
    assert asyncio.run(schema.check_database_schema(metadata_url)) == "future_expand_1"


@pytest.mark.parametrize("field,value", [("generation", 99), ("protocol_version", 99), ("completed_head", "unrelated")])
def test_unrecognized_contract_is_rejected(metadata_url: str, field, value):
    with _connect(metadata_url) as connection:
        raw = connection.execute(
            "SELECT config_value FROM system_config WHERE config_key='schema.compatibility'"
        ).fetchone()[0]
        contract = json.loads(raw)
        contract[field] = value
        connection.execute(
            "UPDATE system_config SET config_value=? WHERE config_key='schema.compatibility'", (json.dumps(contract),)
        )
    with pytest.raises(schema.DatabaseSchemaError) as error:
        asyncio.run(schema.check_database_schema(metadata_url))
    assert error.value.code == "DATABASE_SCHEMA_CONTRACT_INVALID"


def test_inspection_does_not_initialize_database(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    result = asyncio.run(schema.inspect_database(url))
    assert result["compatible"] is False
    assert result["error_code"] == "DATABASE_SCHEMA_NOT_INITIALIZED"
    with _connect(url) as connection:
        assert connection.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall() == []


def test_migration_refuses_concurrent_executor(metadata_url: str):
    from server.db.migration_runner import migration_lock
    from sqlalchemy.ext.asyncio import create_async_engine

    async def run():
        engine = create_async_engine(metadata_url)
        try:
            async with engine.connect() as connection:
                async with migration_lock(connection, metadata_url):
                    with pytest.raises(schema.DatabaseSchemaError) as error:
                        await asyncio.to_thread(schema.migrate_database, metadata_url)
                    assert error.value.code == "DATABASE_MIGRATION_BUSY"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_failed_attempt_is_recorded_and_error_is_sanitized(metadata_url: str, monkeypatch):
    from server.db import migration_runner

    def fail(*args, **kwargs):
        raise RuntimeError("password-do-not-expose")

    monkeypatch.setattr(migration_runner.command, "upgrade", fail)
    with pytest.raises(schema.DatabaseSchemaError) as error:
        schema.migrate_database(metadata_url)
    assert error.value.code == "DATABASE_MIGRATION_FAILED"
    assert "password-do-not-expose" not in str(error.value)
    with _connect(metadata_url) as connection:
        rows = connection.execute(
            "SELECT config_value FROM system_config WHERE config_key LIKE 'schema.migration.%'"
        ).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])["status"] == "FAILED"
        assert "password-do-not-expose" not in rows[0][0]


def test_partial_failure_requires_repair(metadata_url: str, monkeypatch):
    from server.db import migration_runner

    original = migration_runner.command.upgrade

    def fail(config, target):
        c = config.attributes["connection"]
        c.exec_driver_sql("ALTER TABLE agents DROP COLUMN oauth_redirect_uri")
        c.commit()
        raise RuntimeError("interrupted after DDL")

    monkeypatch.setattr(migration_runner.command, "upgrade", fail)
    with pytest.raises(schema.DatabaseSchemaError):
        schema.migrate_database(metadata_url)
    monkeypatch.setattr(migration_runner.command, "upgrade", original)
    with pytest.raises(schema.DatabaseSchemaError) as error:
        schema.migrate_database(metadata_url)
    assert error.value.code == "DATABASE_MIGRATION_REPAIR_REQUIRED"


@pytest.mark.parametrize(
    "value,expected", [("'idle'::character varying", "idle"), ("'ACTIVE'::text", "ACTIVE"), ("false", "0")]
)
def test_postgres_reflected_scalar_defaults(value, expected):
    from server.db.schema_contract import normalize_default

    assert normalize_default(value) == expected


def test_schema_defaults_preserve_text_case_and_literal_parentheses():
    from server.db.schema_contract import normalize_default

    assert normalize_default("'PENDING'::text") != normalize_default("'pending'::text")
    assert normalize_default("'(value)'") == "(value)"
    assert normalize_default("CURRENT_TIMESTAMP()") == "current_timestamp"


def test_manifest_tracks_migration_head():
    from server.db import schema_contract

    assert schema_contract.manifest()["head"] == schema.required_schema_head()


def test_missing_knowledge_scope_column_is_rejected(metadata_url: str):
    with _connect(metadata_url) as connection:
        connection.execute("ALTER TABLE agents DROP COLUMN knowledge_scope_mode")
    with pytest.raises(schema.DatabaseSchemaError) as error:
        asyncio.run(schema.check_database_schema(metadata_url))
    assert error.value.code == "DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN"


def test_existing_baseline_requires_explicit_maintenance_and_replays_safely(tmp_path):
    from alembic import command
    url = f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}"
    command.upgrade(schema._alembic_config(url), "b3c4d5e6f7a8")
    with pytest.raises(schema.DatabaseSchemaError) as error:
        schema.migrate_database(url)
    assert error.value.code == "DATABASE_MAINTENANCE_REQUIRED"
    with _connect(url) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "b3c4d5e6f7a8"
    schema.migrate_database(url, maintenance=True)
    schema.migrate_database(url, maintenance=True)
    assert asyncio.run(schema.check_database_schema(url)) == "c7d8e9f0a1b2"


def test_rejected_prerelease_contract_does_not_rename_objects(tmp_path):
    from alembic import command

    url = f"sqlite+aiosqlite:///{tmp_path / 'prerelease-rejected.db'}"
    command.upgrade(schema._alembic_config(url), "9c1d2e3f4a5b")
    with _connect(url) as connection:
        connection.execute("ALTER TABLE enterprise_directory_memberships RENAME TO identity_source_group_memberships")
    with pytest.raises(schema.DatabaseSchemaError) as error:
        schema.migrate_database(url)
    assert error.value.code == "DATABASE_MAINTENANCE_REQUIRED"
    with _connect(url) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "identity_source_group_memberships" in tables
    assert "enterprise_directory_memberships" not in tables
