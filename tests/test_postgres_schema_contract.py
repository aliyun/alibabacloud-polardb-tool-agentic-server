from sqlalchemy import Enum

from server.models import Base


def test_orm_does_not_require_postgresql_native_enum_types() -> None:
    native_enum_columns = sorted(
        f"{table.name}.{column.name}:{column.type.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Enum) and column.type.native_enum
    )

    assert native_enum_columns == []
