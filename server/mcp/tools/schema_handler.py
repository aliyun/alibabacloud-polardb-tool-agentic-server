"""Handler for the describe_schema MCP tool.

Queries INFORMATION_SCHEMA.TABLES + INFORMATION_SCHEMA.COLUMNS to return
table structure and COMMENTs for cross-session semantic continuity.

All queries that incorporate user-supplied values use parameterized
``cursor.execute(sql, params)`` calls — never string interpolation.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.core.crypto import decrypt
from server.core.responses import error_response
from server.core.sql_executor import SQLExecutionError, encode_cursor
from server.mcp.tools import (
    _decode_cursor,
    get_gateway,
    resolve_target_instance,
)
from server.models import (
    DBAccount,
    Instance,
    InstanceStatus,
    InstanceType,
    User,
)

logger = logging.getLogger(__name__)

# SQL to list tables (parameterized — values supplied via cur.execute params).
_TABLES_SQL = """
SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS, CREATE_TIME
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
{pattern_clause}
ORDER BY TABLE_NAME
LIMIT %s OFFSET %s
"""

# SQL to list columns for a specific table.
_COLUMNS_SQL = """
SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION
"""

_MAX_TABLES_CAP = 100


async def handle_describe_schema(
    user: User,
    session: AsyncSession,
    *,
    instance_id: str | None = None,
    database: str | None = None,
    table_pattern: str | None = None,
    include_columns: bool = True,
    cursor: str | None = None,
    max_tables: int = 20,
    session_factory=None,
    background_tasks=None,
) -> dict:
    """MCP tool: describe_schema — discover tables and their COMMENTs."""

    if isinstance(max_tables, bool) or not isinstance(max_tables, int) or max_tables < 1:
        return error_response("INVALID_ARGUMENT", "max_tables must be a positive integer.")

    result = await resolve_target_instance(
        user, session, instance_id,
        session_factory=session_factory, background_tasks=background_tasks,
    )
    if isinstance(result, dict):
        return result
    target_instance, _accessible = result

    if target_instance.status == InstanceStatus.CREATING:
        return error_response(
            "INSTANCE_STARTING",
            "Instance is starting, please retry in a few seconds.",
        )

    db_account = (await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == target_instance.id,
            DBAccount.user_id == user.id,
        )
    )).scalar_one_or_none()

    if db_account is None:
        if target_instance.type == InstanceType.MULTITENANT:
            from server.core.tenant_provisioner import ensure_tenant
            try:
                db_account = await ensure_tenant(user, target_instance, session)
            except Exception as e:
                return error_response(
                    "TENANT_PROVISION_FAILED",
                    f"Tenant provisioning failed: {e}",
                )
        else:
            return error_response(
                "NO_DB_ACCOUNT",
                "No database account found. Run a SQL statement first to auto-create one.",
            )

    # Resolve database name (mirror run_sql multi-tenant logic).
    password = decrypt(db_account.account_password_enc)
    db_name = database
    if (
        db_name is None
        and target_instance.type == InstanceType.MULTITENANT
        and db_account.tenant_name
    ):
        db_name = f"agentic@{db_account.tenant_name}"

    offset = _decode_cursor(cursor) if cursor else 0
    effective_max = min(max_tables, _MAX_TABLES_CAP)

    # Build the tables query — parameterize the LIKE pattern, never interpolate.
    if table_pattern:
        tables_sql = _TABLES_SQL.format(pattern_clause="AND TABLE_NAME LIKE %s")
        tables_params: list = [db_name or "", table_pattern, effective_max + 1, offset]
    else:
        tables_sql = _TABLES_SQL.format(pattern_clause="")
        tables_params = [db_name or "", effective_max + 1, offset]

    gateway = get_gateway()

    try:
        tables_result = await _execute_parameterized(
            gateway, target_instance, db_account, password, db_name, user.id,
            tables_sql, tables_params,
        )
    except SQLExecutionError as e:
        return error_response(e.code, e.message)

    rows = tables_result.get("rows", [])
    # Request max+1 to detect has_more, then truncate.
    has_more = len(rows) > effective_max
    if has_more:
        rows = rows[:effective_max]

    tables: list[dict] = []
    for row in rows:
        table_name = row[0]
        entry: dict = {
            "table_name": table_name,
            "table_comment": row[1] or "",
            "row_count_estimate": row[2] or 0,
            "created_at": str(row[3]) if row[3] is not None else None,
        }

        if include_columns:
            try:
                cols_result = await _execute_parameterized(
                    gateway, target_instance, db_account, password, db_name,
                    user.id, _COLUMNS_SQL, [db_name or "", table_name],
                )
                entry["columns"] = [
                    {"name": c[0], "type": c[1], "comment": c[2] or ""}
                    for c in cols_result.get("rows", [])
                ]
            except SQLExecutionError:
                entry["columns"] = []

        tables.append(entry)

    result_data: dict = {
        "tables": tables,
        "has_more": has_more,
    }
    if has_more:
        result_data["next_cursor"] = encode_cursor(offset + effective_max)

    return {"content": [{"type": "text", "text": json.dumps(result_data, default=str)}]}


async def _execute_parameterized(
    gateway,
    instance: Instance,
    db_account,
    password: str,
    database: str | None,
    user_id: str,
    sql: str,
    params: list,
) -> dict:
    """Run a parameterized query via the gateway's connection cache.

    Bypasses ``SQLGateway.execute`` (which is geared toward end-user SQL) so we
    can keep ``%s`` placeholders and pass values through asyncmy's parameter
    binding instead of concatenating strings.
    """
    config = get_config()
    conn = await gateway._cache.acquire(
        user_id=user_id,
        instance_id=instance.id,
        host=instance.host or "",
        port=instance.port or 3306,
        user=db_account.account_name,
        password=password,
        database=database,
    )
    discard = False
    try:
        stmt_timeout = config.sql_security.max_timeout_ms / 1000

        async def _run() -> dict:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                columns = (
                    [desc[0] for desc in cur.description]
                    if cur.description else []
                )
                rows = await cur.fetchall()
                return {
                    "columns": columns,
                    "rows": [list(r) for r in rows],
                }

        return await asyncio.wait_for(_run(), timeout=stmt_timeout)
    except asyncio.TimeoutError:
        discard = True
        raise SQLExecutionError("Schema query timed out", "TIMEOUT")
    except SQLExecutionError:
        discard = True
        raise
    except Exception as e:
        discard = True
        raise SQLExecutionError(str(e), "SQL_ERROR")
    finally:
        await gateway._cache.release(user_id, instance.id, conn, discard=discard)
