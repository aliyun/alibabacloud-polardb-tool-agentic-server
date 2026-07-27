from __future__ import annotations

import inspect
from importlib import import_module


class _Dialect:
    name = "mysql"


class _Bind:
    dialect = _Dialect()


class _Operations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

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
