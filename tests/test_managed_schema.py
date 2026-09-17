from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from sqlalchemy import CheckConstraint, Enum, ForeignKeyConstraint, String


def test_password_state_is_nullable_application_string() -> None:
    from server.models.user import PasswordState, User

    column = User.__table__.c.password_state

    assert PasswordState.RESET_REQUIRED.value == "RESET_REQUIRED"
    assert PasswordState.ACTIVE.value == "ACTIVE"
    assert isinstance(column.type, String)
    assert not isinstance(column.type, Enum)
    assert column.type.length == 32
    assert column.nullable is True
    assert not any(
        isinstance(constraint, CheckConstraint)
        for constraint in User.__table__.constraints
    )


def test_legacy_null_password_state_is_active() -> None:
    from server.models.user import AuthProvider, PasswordState, User

    user = User(
        external_id="legacy-admin",
        display_name="Legacy Admin",
        auth_provider=AuthProvider.BUILTIN,
        password_state=None,
    )

    assert user.effective_password_state is PasswordState.ACTIVE


def test_managed_instance_binding_has_no_foreign_keys() -> None:
    from server.models.system_config import ManagedInstanceBinding

    table = ManagedInstanceBinding.__table__

    assert list(table.primary_key.columns.keys()) == ["instance_id"]
    assert table.c.instance_id.type.length == 255
    assert table.c.instance_generation.nullable is False
    assert table.c.bound_at.nullable is False
    assert not any(
        isinstance(constraint, ForeignKeyConstraint)
        for constraint in table.constraints
    )
    assert not table.foreign_keys


def test_operation_receipt_managed_fields_are_nullable() -> None:
    from server.models.system_config import ConfigOperationReceipt

    table = ConfigOperationReceipt.__table__
    expected = {
        "lease_owner",
        "lease_expires_at",
        "instance_id",
        "instance_generation",
    }

    assert expected <= set(table.c.keys())
    assert all(table.c[name].nullable for name in expected)


def test_managed_state_migration_upgrades_sqlite(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    from server.db.schema import check_database_schema, required_schema_head

    database = tmp_path / "managed-state.db"
    database_url = f"sqlite+aiosqlite:///{database}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        user_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(users)")
        }
        receipt_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(config_operation_receipts)"
            )
        }
        binding_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(managed_instance_bindings)"
            )
        }
        binding_foreign_keys = list(
            connection.execute(
                "PRAGMA foreign_key_list(managed_instance_bindings)"
            )
        )

    assert user_columns["password_state"][2].upper() == "VARCHAR(32)"
    assert user_columns["password_state"][3] == 0
    assert {
        "lease_owner",
        "lease_expires_at",
        "instance_id",
        "instance_generation",
    } <= receipt_columns.keys()
    assert {
        "instance_id",
        "instance_generation",
        "bound_at",
        "created_at",
        "updated_at",
    } == binding_columns.keys()
    assert binding_foreign_keys == []
    assert asyncio.run(check_database_schema(database_url)) == required_schema_head()
