"""Reuse verified early-deployment objects without rewriting released revisions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations, ops, toimpl

from server.db.schema_contract import column_type

_PREDECESSOR = "c3d4e5f6a7b8"
_COLUMNS = {
    ("knowledge_resources", "catalog_sync_token"): sa.String(36),
    ("agents", "oauth_redirect_uri"): sa.Text(),
}
_INDEX = "ix_knowledge_resources_catalog_sync_token"


def _legacy_upgrade(operations: Operations) -> bool:
    context = operations.migration_context
    return (
        not context.as_sql
        and getattr(context.opts.get("fn"), "__name__", None) == "upgrade"
        and context.get_current_heads() == (_PREDECESSOR,)
    )


def _preflight(operations: Operations) -> tuple[set[tuple[str, str]], bool]:
    """Inspect all reusable objects before any of the original DDL runs."""
    inspector = sa.inspect(operations.get_bind())
    existing_columns: set[tuple[str, str]] = set()
    for (table, name), expected_type in _COLUMNS.items():
        existing = next(
            (column for column in inspector.get_columns(table) if column["name"] == name),
            None,
        )
        if existing is None:
            continue
        if (
            column_type(existing["type"]) != column_type(expected_type)
            or not existing["nullable"]
            or existing["default"] is not None
        ):
            raise RuntimeError(f"Incompatible existing column: {table}.{name}")
        existing_columns.add((table, name))

    index = next(
        (item for item in inspector.get_indexes("knowledge_resources") if item["name"] == _INDEX),
        None,
    )
    if index is not None and (
        index["column_names"] != ["catalog_sync_token"]
        or index["unique"]
        or index.get("column_sorting")
        or any(
            value is not None and value is not False
            and not (isinstance(value, (list, dict, tuple)) and not value)
            for value in index.get("dialect_options", {}).values()
        )
    ):
        raise RuntimeError(f"Incompatible existing index: {_INDEX}")
    return existing_columns, index is not None


@Operations.implementation_for(ops.AddColumnOp, replace=True)
def add_column(operations: Operations, operation: ops.AddColumnOp) -> None:
    key = (operation.table_name, operation.column.name)
    expected_type = _COLUMNS.get(key)
    if operation.schema is None and expected_type is not None and _legacy_upgrade(operations):
        existing, _ = _preflight(operations)
        column = operation.column
        if (
            key in existing
            and column_type(column.type) == column_type(expected_type)
            and column.nullable
            and column.server_default is None
            and not column.constraints
            and not column.foreign_keys
            and not column.primary_key
            and not column.unique
            and not column.index
        ):
            return
    toimpl.add_column(operations, operation)


@Operations.implementation_for(ops.CreateIndexOp, replace=True)
def create_index(operations: Operations, operation: ops.CreateIndexOp) -> None:
    if (
        operation.schema is None
        and operation.table_name == "knowledge_resources"
        and operation.index_name == _INDEX
        and _legacy_upgrade(operations)
    ):
        _, existing = _preflight(operations)
        if existing and operation.columns == ["catalog_sync_token"] and not operation.unique and not operation.kw:
            return
    toimpl.create_index(operations, operation)
