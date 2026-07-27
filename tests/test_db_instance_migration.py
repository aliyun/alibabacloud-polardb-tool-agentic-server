from __future__ import annotations

import base64
import sqlite3
import warnings
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SAWarning

from server.config import reset_config

PRE_AGENTIC_DB_REVISION = "ad71f04a14b5"
AGENTIC_DB_REVISION = "c0f1a2b3c4d5"
HEAD_REVISION = "d4e5f6a7b8c9"
ENCRYPTION_KEY = base64.b64encode(
    b"01234567890123456789012345678901"
).decode()
NEW_TABLES = {
    "agents",
    "agent_api_tokens",
    "agent_token_reveal_limits",
    "instance_credentials",
    "agent_instance_bindings",
    "agent_instance_binding_capabilities",
    "user_instance_binding_capabilities",
    "provisioning_backends",
    "agent_provisioning_bindings",
    "provisioning_backend_health",
    "provisioning_capacities",
    "db_instance_resources",
    "secret_reveal_limits",
    "system_config",
    "config_bootstrap_claim",
    "config_operation_receipts",
}
REMOVED_TABLES = {
    "db_accounts",
    "db_backend_health",
    "db_instance_leases",
    "db_lease_capacities",
    "system_settings",
}


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(Path(__file__).parents[1] / "alembic.ini"))


def _upgrade(monkeypatch, database: Path, revision: str) -> None:
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", ENCRYPTION_KEY)
    reset_config()
    command.upgrade(_alembic_config(), revision)
    reset_config()


def _check(monkeypatch, database: Path) -> None:
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", ENCRYPTION_KEY)
    reset_config()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "error",
            message="Cannot correctly sort tables.*",
            category=SAWarning,
        )
        command.check(_alembic_config())
    reset_config()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_target_migration_has_single_head_and_new_tables(tmp_path, monkeypatch):
    database = tmp_path / "target.db"

    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        assert NEW_TABLES <= _tables(connection)
        assert REMOVED_TABLES.isdisjoint(_tables(connection))
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
    assert ScriptDirectory.from_config(_alembic_config()).get_heads() == [HEAD_REVISION]


def test_target_migration_replaces_legacy_instance_and_audit_columns(tmp_path, monkeypatch):
    database = tmp_path / "target-columns.db"

    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        instance_columns = {row[1] for row in connection.execute("PRAGMA table_info(instances)")}
        audit_columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_logs)")}
        token_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_api_tokens)")}
        backend_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(provisioning_backends)"
            )
        }

    assert {"engine", "topology", "allocation_mode", "usage"} <= instance_columns
    assert "type" not in instance_columns
    assert {"actor_user_id", "actor_agent_id", "target_type", "target_id"} <= audit_columns
    assert "user_id" not in audit_columns
    assert "token_ciphertext" in token_columns
    assert "name" not in token_columns
    assert "config_revision" in backend_columns


def test_target_migration_matches_orm_metadata(tmp_path, monkeypatch):
    database = tmp_path / "target-check.db"

    _upgrade(monkeypatch, database, "head")

    _check(monkeypatch, database)


def test_target_migration_rejects_non_persistable_binding_capabilities(tmp_path, monkeypatch):
    database = tmp_path / "target-capabilities.db"
    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        for table, constraint_name in [
            (
                "user_instance_binding_capabilities",
                "ck_user_instance_binding_capabilities_value",
            ),
            (
                "agent_instance_binding_capabilities",
                "ck_agent_instance_binding_capabilities_value",
            ),
        ]:
            for capability in [
                "unknown:capability",
                "db_instance:create",
                "db_instance:delete",
            ]:
                with pytest.raises(sqlite3.IntegrityError, match=constraint_name):
                    connection.execute(
                        f"INSERT INTO {table} (binding_id, capability) VALUES (?, ?)",
                        ("missing-binding", capability),
                    )
