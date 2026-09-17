from sqlalchemy import Enum
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from server.models import Base


def test_orm_does_not_require_postgresql_native_enum_types() -> None:
    native_enum_columns = sorted(
        f"{table.name}.{column.name}:{column.type.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Enum) and column.type.native_enum
    )

    assert native_enum_columns == []


def test_managed_state_schema_compiles_for_postgresql_without_foreign_keys() -> None:
    table = Base.metadata.tables["managed_instance_bindings"]
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect())).upper()

    assert "INSTANCE_GENERATION BIGINT NOT NULL" in ddl
    assert "FOREIGN KEY" not in ddl


def test_credential_epoch_schema_compiles_for_postgresql() -> None:
    table = Base.metadata.tables["users"]
    column = table.c.credential_epoch
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect())).upper()

    assert column.nullable is False
    assert str(column.server_default.arg) == "1"
    assert not column.foreign_keys
    assert "CREDENTIAL_EPOCH INTEGER DEFAULT '1' NOT NULL" in ddl
