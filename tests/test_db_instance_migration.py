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
DEDICATED_POOL_BASE_REVISION = "d4e5f6a7b8c9"
DEDICATED_POOL_REVISION = "f3a4b5c6d7e8"
HEAD_REVISION = "c1d2e3f4a5b6"
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
    "permission_templates",
    "permission_template_revisions",
    "permission_sync_jobs",
    "permission_sync_targets",
    "dedicated_pools",
    "dedicated_pool_members",
    "provisioning_operation_budgets",
    "dedicated_worker_heartbeats",
    "polarrag_instances",
    "polarrag_spaces",
    "knowledge_resources",
    "enterprise_principal_assignments",
    "agent_polarrag_instance_bindings",
    "agent_user_assignments",
    "agent_user_tokens",
    "agent_group_assignments",
    "polarrag_upload_sessions",
}
REMOVED_TABLES = {
    "db_accounts",
    "db_backend_health",
    "db_instance_leases",
    "db_lease_capacities",
    "system_settings",
}
POLARRAG_TABLES = {
    "polarrag_instances",
    "polarrag_spaces",
    "knowledge_resources",
    "enterprise_principal_assignments",
    "agent_polarrag_instance_bindings",
    "agent_user_assignments",
    "agent_user_tokens",
    "agent_group_assignments",
    "polarrag_upload_sessions",
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


def _downgrade(monkeypatch, database: Path, revision: str) -> None:
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", ENCRYPTION_KEY)
    reset_config()
    command.downgrade(_alembic_config(), revision)
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


def test_polarrag_migration_downgrades_and_reupgrades_cleanly(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "polarrag-roundtrip.db"
    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        assert POLARRAG_TABLES <= _tables(connection)

    _downgrade(monkeypatch, database, "d4e5f6a7b8c9")
    with sqlite3.connect(database) as connection:
        assert POLARRAG_TABLES.isdisjoint(_tables(connection))

    _upgrade(monkeypatch, database, "head")
    with sqlite3.connect(database) as connection:
        assert POLARRAG_TABLES <= _tables(connection)


def test_native_polarrag_principal_constraint_round_trip(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "native-principal-roundtrip.db"
    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'enterprise_principal_assignments'"
        ).fetchone()[0]
        assert "'polarrag'" in table_sql
        assert "ck_enterprise_principal_native_user" in table_sql
        assert "ck_enterprise_principal_native_admin" in table_sql

    _downgrade(monkeypatch, database, "b0c1d2e3f4a5")
    with sqlite3.connect(database) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'enterprise_principal_assignments'"
        ).fetchone()[0]
        assert "'polarrag'" not in table_sql
        assert "ck_enterprise_principal_native_user" not in table_sql
        assert "ck_enterprise_principal_native_admin" not in table_sql

    _upgrade(monkeypatch, database, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD_REVISION,)


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
        resource_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(db_instance_resources)"
            )
        }
        member_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(dedicated_pool_members)"
            )
        }
        pool_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(dedicated_pools)"
            )
        }
        provisioning_binding_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(agent_provisioning_bindings)"
            )
        }

    assert {"engine", "topology", "allocation_mode", "usage"} <= instance_columns
    assert "type" not in instance_columns
    assert {"actor_user_id", "actor_agent_id", "target_type", "target_id"} <= audit_columns
    assert "user_id" not in audit_columns
    assert "token_ciphertext" in token_columns
    assert "name" not in token_columns
    assert {
        "config_revision",
        "backend_type",
        "dedicated_pool_id",
        "delete_cooldown_duration_hours",
        "permission_template_revision_id",
    } <= backend_columns
    assert {
        "provisioning_mode",
        "allocated_instance_id",
        "permission_template_id",
        "permission_template_revision_id",
        "permission_snapshot_json",
        "delete_requested_at",
        "disconnected_at",
        "effective_delete_cooldown_duration_hours",
        "cooldown_until",
        "reclaim_policy",
        "delete_step",
        "restore_source_status",
        "restore_failure_reason",
    } <= resource_columns
    assert {
        "preparation_step",
        "purchase_token",
        "cloud_request_id",
        "agentic_db_cluster_id",
    } <= member_columns
    assert {
        "purchase_profile_id",
        "purchase_profile_revision",
        "storage_type",
    } <= pool_columns
    assert "routing_order" in provisioning_binding_columns


def test_resource_status_model_covers_create_delete_cooldown_and_restore():
    from server.models import DBInstanceStatus

    assert {status.value for status in DBInstanceStatus} == {
        "creating",
        "ready",
        "failed",
        "deleting",
        "cooling_down",
        "restoring",
        "deleted",
        "delete_failed",
    }


def test_dedicated_pool_schema_is_delivered_by_one_revision():
    script = ScriptDirectory.from_config(_alembic_config())

    revisions = [
        migration.revision
        for migration in script.iterate_revisions(
            DEDICATED_POOL_REVISION,
            DEDICATED_POOL_BASE_REVISION,
        )
    ]

    assert revisions == [DEDICATED_POOL_REVISION]


def test_consolidated_migration_does_not_adopt_legacy_pooled_instances(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "single-pool-ownership.db"
    _upgrade(monkeypatch, database, DEDICATED_POOL_BASE_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for instance_id, cluster_id in (
            ("member-instance", "pc-member"),
            ("legacy-instance", "pc-legacy-unlinked"),
        ):
            connection.execute(
                "INSERT INTO instances("
                "id, cluster_id, name, engine, topology, allocation_mode, "
                "status, quota_held, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instance_id,
                    cluster_id,
                    instance_id,
                    "POLARDB_MYSQL",
                    "SINGLE_TENANT",
                    "POOLED",
                    "ACTIVE",
                    0,
                    "2026-08-11T00:00:00Z",
                ),
            )
        connection.commit()

    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        modes = dict(
            connection.execute(
                "SELECT id, allocation_mode FROM instances ORDER BY id"
            ).fetchall()
        )
        member_count = connection.execute(
            "SELECT COUNT(*) FROM dedicated_pool_members"
        ).fetchone()

    assert modes == {
        "legacy-instance": "POOLED",
        "member-instance": "POOLED",
    }
    assert member_count == (0,)


def test_hot_pool_migration_backfills_legacy_rows_without_touching_secrets(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "hot-pool-backfill.db"
    _upgrade(monkeypatch, database, "d4e5f6a7b8c9")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO instances("
            "id, cluster_id, name, engine, topology, allocation_mode, "
            "status, quota_held, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "instance-1",
                "pc-legacy",
                "legacy",
                "POLARDB_MYSQL",
                "MULTITENANT",
                "REGISTERED",
                "ACTIVE",
                0,
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO agents(id, name, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "agent-1",
                "legacy-agent",
                "ACTIVE",
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO instance_credentials("
            "id, instance_id, name, purpose, capability, "
            "username_ciphertext, password_ciphertext, status, version, "
            "created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "credential-1",
                "instance-1",
                "lifecycle",
                "PROVISIONING_ADMIN",
                "ADMIN",
                "unchanged-username-ciphertext",
                "unchanged-password-ciphertext",
                "ACTIVE",
                1,
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO provisioning_backends("
            "id, instance_id, admin_credential_id, status, priority, "
            "max_active_resources, resource_min_cpu, resource_max_cpu, "
            "ddl_concurrency, config_revision, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "backend-1",
                "instance-1",
                "credential-1",
                "ACTIVE",
                0,
                10,
                0,
                2,
                4,
                1,
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO db_instance_resources("
            "id, owner_agent_id, backend_id, client_token, "
            "request_fingerprint, fingerprint_version, engine, status, "
            "tenant_name, provisioning_step, cleanup_step, "
            "cleanup_required, retry_count, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "resource-1",
                "agent-1",
                "backend-1",
                "legacy-token",
                "0" * 64,
                1,
                "POLARDB_MYSQL",
                "READY",
                "t123456789",
                "VERIFIED",
                "PENDING",
                0,
                0,
                "2026-08-10T00:00:00Z",
            ),
        )

    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        backend = connection.execute(
            "SELECT backend_type, instance_id, dedicated_pool_id, "
            "delete_cooldown_duration_hours, "
            "permission_template_revision_id "
            "FROM provisioning_backends WHERE id = ?",
            ("backend-1",),
        ).fetchone()
        resource = connection.execute(
            "SELECT provisioning_mode, allocated_instance_id, "
            "permission_snapshot_json FROM db_instance_resources "
            "WHERE id = ?",
            ("resource-1",),
        ).fetchone()
        credential = connection.execute(
            "SELECT username_ciphertext, password_ciphertext "
            "FROM instance_credentials WHERE id = ?",
            ("credential-1",),
        ).fetchone()

    assert backend == (
        "MULTITENANT",
        "instance-1",
        None,
        None,
        "builtin-mysql-default-v1",
    )
    assert resource == (
        "MULTITENANT",
        None,
        '{"grant_option":true,"legacy":true,'
        '"privileges":["ALL PRIVILEGES"],"scope":"tenant"}',
    )
    assert credential == (
        "unchanged-username-ciphertext",
        "unchanged-password-ciphertext",
    )


def test_consolidated_migration_keeps_existing_multitenant_routes_unordered(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "multitenant-route-upgrade.db"
    _upgrade(monkeypatch, database, DEDICATED_POOL_BASE_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(id, external_id, display_name, auth_provider, "
            "role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "admin-route",
                "admin-route",
                "Route Admin",
                "BUILTIN",
                "ADMIN",
                "ACTIVE",
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO agents(id, name, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "agent-route",
                "route-agent",
                "ACTIVE",
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO instances("
            "id, cluster_id, name, engine, topology, allocation_mode, "
            "status, quota_held, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "instance-route",
                "pc-route",
                "route-instance",
                "POLARDB_MYSQL",
                "MULTITENANT",
                "REGISTERED",
                "ACTIVE",
                0,
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO instance_credentials("
            "id, instance_id, name, purpose, capability, "
            "username_ciphertext, password_ciphertext, status, version, "
            "created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "credential-route",
                "instance-route",
                "route-admin",
                "PROVISIONING_ADMIN",
                "ADMIN",
                "username-ciphertext",
                "password-ciphertext",
                "ACTIVE",
                1,
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO provisioning_backends("
            "id, instance_id, admin_credential_id, status, priority, "
            "max_active_resources, resource_min_cpu, resource_max_cpu, "
            "ddl_concurrency, config_revision, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "backend-route",
                "instance-route",
                "credential-route",
                "ACTIVE",
                10,
                10,
                0,
                2,
                4,
                1,
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO agent_provisioning_bindings("
            "id, agent_id, backend_id, enabled, created_by_user_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "binding-route",
                "agent-route",
                "backend-route",
                1,
                "admin-route",
                "2026-08-10T00:00:00Z",
            ),
        )
        connection.commit()

    _upgrade(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        route = connection.execute(
            "SELECT routing_order FROM agent_provisioning_bindings "
            "WHERE id = ?",
            ("binding-route",),
        ).fetchone()
        template_name = connection.execute(
            "SELECT name FROM permission_templates WHERE id = ?",
            ("builtin-mysql-default",),
        ).fetchone()

    assert route == (None,)
    assert template_name == ("Agent MySQL default permissions",)


def test_target_migration_matches_orm_metadata(tmp_path, monkeypatch):
    database = tmp_path / "target-check.db"

    _upgrade(monkeypatch, database, "head")

    _check(monkeypatch, database)


def test_consolidated_migration_can_reupgrade_after_downgrade(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "permission-policy-reupgrade.db"

    _upgrade(monkeypatch, database, "head")
    monkeypatch.setenv(
        "PAS_DATABASE_URL", f"sqlite+aiosqlite:///{database}"
    )
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", ENCRYPTION_KEY)
    reset_config()
    command.downgrade(_alembic_config(), DEDICATED_POOL_BASE_REVISION)
    command.upgrade(_alembic_config(), "head")
    reset_config()

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, privileges_json, grant_option "
            "FROM permission_template_revisions "
            "WHERE id = ?",
            ("builtin-mysql-default-v1",),
        ).fetchall()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()

    assert len(rows) == 1
    assert rows[0][2] == 0
    assert revision == (HEAD_REVISION,)


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
