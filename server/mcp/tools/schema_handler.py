"""Handler for the describe_schema MCP tool.

Queries INFORMATION_SCHEMA.TABLES + INFORMATION_SCHEMA.COLUMNS to return
table structure and COMMENTs for cross-session semantic continuity.

All queries that incorporate user-supplied values use parameterized
``cursor.execute(sql, params)`` calls — never string interpolation.
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from server.core.audit_logger import log_audit
from server.core.crypto import decrypt
from server.core.responses import error_response
from server.core.sql_gateway import SQLGateway
from server.core.sql_executor import SQLExecutionError, encode_cursor
from server.mcp.tools import (
    _decode_cursor,
    get_gateway,
    resolve_target_instance,
)
from server.mcp.tools.agent_sql_access import resolve_agent_sql_access
from server.mcp.tools.handlers import (
    SQLActor,
    _actor_audit_fields,
    _actor_cache_key,
    _is_agent_actor,
    _resolve_user_sql_credential,
)
from server.models import (
    AuditStatus,
    Instance,
    InstanceStatus,
    InstanceTopology,
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
    user: SQLActor,
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
    started_at = time.perf_counter()

    if isinstance(max_tables, bool) or not isinstance(max_tables, int) or max_tables < 1:
        return error_response("INVALID_ARGUMENT", "max_tables must be a positive integer.")

    if _is_agent_actor(user):
        credential_result = await resolve_agent_sql_access(
            user,
            session,
            instance_id=instance_id,
            database=database,
        )
        if isinstance(credential_result, dict):
            return credential_result
        target_instance = credential_result.instance
        credential = credential_result.credential
        db_name = credential_result.database
        public_instance_id = credential_result.public_instance_id
        target_type = (
            "db_instance_resource"
            if credential_result.source == "provisioned"
            else "instance"
        )
    else:
        result = await resolve_target_instance(
            user,
            session,
            instance_id,
            session_factory=session_factory,
            background_tasks=background_tasks,
        )
        if isinstance(result, dict):
            return result
        target_instance, _accessible = result
        if target_instance.status == InstanceStatus.CREATING:
            return error_response(
                "INSTANCE_STARTING",
                "Instance is starting, please retry in a few seconds.",
            )
        credential_result = await _resolve_user_sql_credential(user, target_instance, session)
        if isinstance(credential_result, dict):
            return credential_result
        binding = credential_result.binding
        credential = credential_result.credential
        db_name = database
        public_instance_id = target_instance.id
        target_type = "instance"
        if db_name is None and target_instance.topology == InstanceTopology.MULTITENANT and binding.tenant_name:
            db_name = f"agentic@{binding.tenant_name}"

    if target_instance.status == InstanceStatus.CREATING:
        return error_response(
            "INSTANCE_STARTING",
            "Instance is starting, please retry in a few seconds.",
        )

    # Resolve database name (mirror run_sql multi-tenant logic).
    if credential.username_ciphertext is None or credential.password_ciphertext is None:
        return error_response("NO_DB_ACCOUNT", "Database credential is unavailable.")
    username = decrypt(credential.username_ciphertext)
    password = decrypt(credential.password_ciphertext)

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
            gateway,
            target_instance,
            username,
            password,
            db_name,
            _actor_cache_key(user),
            public_instance_id,
            tables_sql,
            tables_params,
        )
    except SQLExecutionError as e:
        await log_audit(
            session,
            instance_id=target_instance.id,
            action="describe_schema",
            status=AuditStatus.ERROR,
            error_message=e.message,
            error_code=e.code,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            target_type=target_type,
            target_id=public_instance_id,
            **_actor_audit_fields(user),
        )
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
                    gateway,
                    target_instance,
                    username,
                    password,
                    db_name,
                    _actor_cache_key(user),
                    public_instance_id,
                    _COLUMNS_SQL,
                    [db_name or "", table_name],
                )
                entry["columns"] = [
                    {"name": c[0], "type": c[1], "comment": c[2] or ""} for c in cols_result.get("rows", [])
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

    await log_audit(
        session,
        instance_id=target_instance.id,
        action="describe_schema",
        status=AuditStatus.SUCCESS,
        row_count=len(tables),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        target_type=target_type,
        target_id=public_instance_id,
        **_actor_audit_fields(user),
    )
    return {"content": [{"type": "text", "text": json.dumps(result_data, default=str)}]}


async def _execute_parameterized(
    gateway: SQLGateway,
    instance: Instance,
    username: str,
    password: str,
    database: str | None,
    user_id: str,
    public_instance_id: str,
    sql: str,
    params: list,
) -> dict:
    """Run trusted schema SQL through the gateway's sanitized public API."""
    return await gateway.execute_parameterized(
        host=instance.host or "",
        port=instance.port or 3306,
        user=username,
        password=password,
        sql=sql,
        params=params,
        database=database,
        user_id=user_id,
        instance_id=public_instance_id,
    )
