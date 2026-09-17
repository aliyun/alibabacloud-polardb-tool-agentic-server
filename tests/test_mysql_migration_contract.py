from __future__ import annotations

import inspect
from io import StringIO
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql

from server.db.mysql_check_compat import (
    CompatibleMySQLImpl,
    supports_check_constraints,
)
from server.models import Base


class _Dialect:
    name = "mysql"


class _Bind:
    dialect = _Dialect()


class _Operations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    @staticmethod
    def get_bind() -> _Bind:
        return _Bind()

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        *,
        unique: bool,
    ) -> None:
        assert columns == ["owner_user_id"]
        assert unique is False
        self.calls.append(("create", name, table_name))

    def drop_index(self, name: str, *, table_name: str) -> None:
        self.calls.append(("drop", name, table_name))

    def add_column(self, table_name: str, column) -> None:
        self.calls.append(("add_column", table_name, column.name))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.calls.append(("drop_column", table_name, column_name))


class _Inspector:
    @staticmethod
    def get_foreign_keys(table_name: str) -> list[dict]:
        if table_name == "audit_logs":
            return [
                {
                    "name": "audit_logs_ibfk_1",
                    "constrained_columns": ["user_id"],
                }
            ]
        return [
            {
                "name": "user_instance_bindings_ibfk_1",
                "constrained_columns": ["db_account_id"],
            }
        ]


class _BatchOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def drop_constraint(self, name: str, *, type_: str) -> None:
        self.calls.append((name, type_))


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ((8, 0, 13), False),
        ((8, 0, 15), False),
        ((8, 0, 16), True),
        ((8, 4, 0), True),
    ],
)
def test_mysql_check_constraint_support_boundary(
    version: tuple[int, ...],
    supported: bool,
) -> None:
    dialect = mysql.dialect()
    dialect.server_version_info = version

    assert supports_check_constraints(dialect) is supported


def _migration_sql(
    module_name: str,
    operation_name: str,
    server_version: tuple[int, ...],
    monkeypatch,
) -> str:
    migration = import_module(
        f"server.db.migrations.versions.{module_name}"
    )
    output = StringIO()
    dialect = mysql.dialect()
    dialect.server_version_info = server_version
    context = MigrationContext.configure(
        dialect=dialect,
        opts={"as_sql": True, "output_buffer": output},
    )
    assert isinstance(context.impl, CompatibleMySQLImpl)
    monkeypatch.setattr(migration, "op", Operations(context))

    getattr(migration, operation_name)()
    return output.getvalue()


def test_native_principal_migration_skips_checks_on_mysql_8_0_13(
    monkeypatch,
) -> None:
    sql = _migration_sql(
        "c1d2e3f4a5b6_add_native_polarrag_principals",
        "upgrade",
        (8, 0, 13),
        monkeypatch,
    )

    assert "CHECK" not in sql.upper()


def test_identity_source_all_migration_keeps_column_ddl_on_mysql_8_0_13(
    monkeypatch,
) -> None:
    sql = _migration_sql(
        "f5a6b7c8d9e0_add_identity_source_all_agent_access",
        "upgrade",
        (8, 0, 13),
        monkeypatch,
    )

    assert "CHECK" not in sql.upper()
    assert "VARCHAR(32)" in sql.upper()


def test_native_principal_migration_keeps_checks_on_mysql_8_0_16(
    monkeypatch,
) -> None:
    sql = _migration_sql(
        "c1d2e3f4a5b6_add_native_polarrag_principals",
        "upgrade",
        (8, 0, 16),
        monkeypatch,
    )

    assert "DROP CHECK" in sql.upper()
    assert "CHECK" in sql.upper()


def test_agent_access_migration_preserves_mysql_owner_fk_index(
    monkeypatch,
) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "9f1a2b3c4d5e_add_agentic_db_leases"
    )
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration._create_mysql_owner_fk_support_index()
    migration._drop_mysql_owner_fk_support_index()

    assert operations.calls == [
        (
            "create",
            "ix_instances_owner_user_id_fk_support",
            "instances",
        ),
        (
            "drop",
            "ix_instances_owner_user_id_fk_support",
            "instances",
        ),
    ]


def test_agent_access_migration_drops_mysql_fk_before_column(
    monkeypatch,
) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "9f1a2b3c4d5e_add_agentic_db_leases"
    )
    operations = _Operations()
    batch_operations = _BatchOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _Inspector(),
    )

    migration._drop_mysql_db_account_foreign_key(batch_operations)

    assert batch_operations.calls == [
        ("user_instance_bindings_ibfk_1", "foreignkey")
    ]

    audit_batch_operations = _BatchOperations()
    migration._drop_mysql_audit_user_foreign_key(
        audit_batch_operations
    )

    assert audit_batch_operations.calls == [
        ("audit_logs_ibfk_1", "foreignkey")
    ]


def test_provisioning_backend_uses_one_instance_unique_index() -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "9f1a2b3c4d5e_add_agentic_db_leases"
    )
    upgrade_source = inspect.getsource(migration.upgrade)
    backend_schema = upgrade_source.split(
        '"provisioning_backends"', maxsplit=1
    )[1].split('"db_instance_resources"', maxsplit=1)[0]

    assert 'sa.UniqueConstraint("instance_id")' not in backend_schema
    assert '"ix_provisioning_backends_instance_id"' in backend_schema


@pytest.mark.parametrize(
    ("module_name", "operation_name"),
    [
        (
            "d2e3f4a5b6c7_add_enterprise_identity_sources",
            "upgrade",
        ),
        (
            "d2e3f4a5b6c7_add_enterprise_identity_sources",
            "downgrade",
        ),
        (
            "f5a6b7c8d9e0_add_identity_source_all_agent_access",
            "upgrade",
        ),
        (
            "f5a6b7c8d9e0_add_identity_source_all_agent_access",
            "downgrade",
        ),
    ],
)
def test_agent_group_migrations_use_native_mysql_alter(
    module_name: str,
    operation_name: str,
    monkeypatch,
) -> None:
    migration = import_module(
        f"server.db.migrations.versions.{module_name}"
    )
    output = StringIO()
    context = MigrationContext.configure(
        url="mysql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    getattr(migration, operation_name)()

    sql = output.getvalue()
    assert "_alembic_tmp_agent_group_assignments" not in sql
    assert "ALTER TABLE agent_group_assignments" in sql


def test_managed_state_schema_compiles_for_mysql_without_foreign_keys() -> None:
    from sqlalchemy.schema import CreateTable

    table = Base.metadata.tables["managed_instance_bindings"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()

    assert "INSTANCE_GENERATION BIGINT NOT NULL" in ddl
    assert "FOREIGN KEY" not in ddl


def test_credential_epoch_migration_is_expand_only(monkeypatch) -> None:
    migration = import_module(
        "server.db.migrations.versions."
        "f6a7b8c9d0e1_add_user_credential_epoch"
    )
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()
    migration.downgrade()

    assert operations.calls == [
        ("add_column", "users", "credential_epoch"),
        ("drop_column", "users", "credential_epoch"),
    ]


def test_credential_epoch_schema_compiles_for_mysql_without_foreign_keys() -> None:
    from sqlalchemy.schema import CreateTable

    table = Base.metadata.tables["users"]
    column = table.c.credential_epoch
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()

    assert column.nullable is False
    assert str(column.server_default.arg) == "1"
    assert not column.foreign_keys
    assert "CREDENTIAL_EPOCH INTEGER NOT NULL" in ddl
