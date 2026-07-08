from __future__ import annotations

import asyncio
import logging
import re

import sqlparse
from sqlparse import tokens as sql_tokens

from server.config import get_config
from server.core.connection_cache import ConnectionCache
from server.core.sql_executor import SQLExecutionError, is_select, apply_pagination, encode_cursor

logger = logging.getLogger(__name__)


async def _exec_simple(conn, sql: str, params: tuple | None = None) -> None:
    async with conn.cursor() as cur:
        if params is None:
            await cur.execute(sql)
        else:
            await cur.execute(sql, params)


def _expand_executable_comments(sql: str) -> str:
    return re.sub(r"/\*!\d*\s*(.*?)\*/", r" \1 ", sql, flags=re.DOTALL)


def _strip_string_literals(sql: str) -> str:
    return re.sub(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"", "", sql)


def _mentions_session_branch(sql: str) -> bool:
    sql = _strip_string_literals(sql)
    sql = _expand_executable_comments(sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"(--[^\r\n]*|#[^\r\n]*)", "", sql)
    normalized = "".join(sql.lower().split()).replace("`", "")
    return any(pattern in normalized for pattern in (
        "@@session.branch",
        "@@local.branch",
        "@@branch",
        "setsessionbranch=",
        "setlocalbranch=",
        "setbranch=",
    ))


def _changes_database_context(sql: str) -> bool:
    sql = _expand_executable_comments(sql)
    try:
        for stmt in sqlparse.parse(sql):
            for token in stmt.flatten():
                if token.is_whitespace or token.ttype in sql_tokens.Comment:
                    continue
                if token.ttype is sql_tokens.Keyword and token.normalized == "USE":
                    return True
                break
    except Exception:
        pass
    return False


class SQLGateway:
    def __init__(self, cache: ConnectionCache) -> None:
        self._cache = cache

    async def execute(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        sql: str,
        database: str | None,
        user_id: str,
        instance_id: str,
        max_rows: int = 1000,
        offset: int = 0,
        read_only: bool = False,
        branch: str | None = None,
    ) -> dict:
        config = get_config()
        paginated_sql = apply_pagination(sql, max_rows + 1, offset)
        conn = await self._cache.acquire(
            user_id=user_id, instance_id=instance_id,
            host=host, port=port, user=user, password=password, database=database,
        )
        discard = _changes_database_context(sql) or _mentions_session_branch(sql)
        branch_set = False
        try:
            # Always set READ WRITE — read-only enforcement is done at the
            # application layer (handler rejects write SQL for READONLY users).
            # This avoids SET SESSION TRANSACTION READ ONLY, which causes
            # @@session.transaction_read_only = 1 and misleads AI agents that
            # probe this variable to decide whether they can write.
            await _exec_simple(conn, "SET SESSION TRANSACTION READ WRITE")
            if branch is not None:
                await _exec_simple(conn, "SET @@session.branch = %s", (branch,))
                branch_set = True
            if is_select(sql):
                await _exec_simple(conn, f"SET max_execution_time={config.sql_security.timeout_ms}")

            stmt_timeout = config.sql_security.max_timeout_ms / 1000
            result = await asyncio.wait_for(
                self._execute_query(conn, paginated_sql, max_rows, offset),
                timeout=stmt_timeout,
            )
            return result
        except asyncio.TimeoutError:
            discard = True
            raise SQLExecutionError("Query timed out", "TIMEOUT")
        except SQLExecutionError:
            discard = True
            raise
        except Exception:
            discard = True
            raise
        finally:
            if branch_set and not discard:
                try:
                    await _exec_simple(conn, "SET @@session.branch = ''")
                except Exception:
                    discard = True
            await self._cache.release(user_id, instance_id, conn, discard=discard)

    async def execute_transaction(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        sql_statements: list[str],
        database: str | None,
        user_id: str,
        instance_id: str,
        read_only: bool = False,
    ) -> list[dict]:
        config = get_config()
        conn = await self._cache.acquire(
            user_id=user_id, instance_id=instance_id,
            host=host, port=port, user=user, password=password, database=database,
        )
        session_state_touched = any(
            _changes_database_context(stmt) or _mentions_session_branch(stmt)
            for stmt in sql_statements
        )
        discard = session_state_touched
        try:
            # Always set READ WRITE — read-only enforcement is done at the
            # application layer (handler rejects write SQL for READONLY users).
            await _exec_simple(conn, "SET SESSION TRANSACTION READ WRITE")

            per_txn_timeout = min(
                (config.sql_security.max_timeout_ms / 1000) * len(sql_statements),
                config.sql_security.max_timeout_ms / 1000 * 10,
            )

            async def _run_transaction() -> list[dict]:
                await _exec_simple(conn, "BEGIN")
                results = []
                for stmt in sql_statements:
                    result = await self._execute_query(conn, stmt, max_rows=1000, offset=0)
                    results.append(result)
                await _exec_simple(conn, "COMMIT")
                return results

            return await asyncio.wait_for(_run_transaction(), timeout=per_txn_timeout)

        except (asyncio.TimeoutError, Exception) as e:
            discard = True
            try:
                await _exec_simple(conn, "ROLLBACK")
                if not isinstance(e, asyncio.TimeoutError):
                    discard = session_state_touched
            except Exception:
                pass
            if isinstance(e, asyncio.TimeoutError):
                raise SQLExecutionError("Transaction timed out", "TIMEOUT")
            raise
        finally:
            await self._cache.release(user_id, instance_id, conn, discard=discard)

    async def _execute_query(self, conn, sql: str, max_rows: int, offset: int) -> dict:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = await cur.fetchall()
            rows_list = [list(r) for r in rows]
            truncated = len(rows_list) > max_rows
            if truncated:
                rows_list = rows_list[:max_rows]
            result: dict = {
                "columns": columns,
                "rows": rows_list,
                "row_count": len(rows_list),
                "truncated": truncated,
            }
            if truncated:
                result["next_cursor"] = encode_cursor(offset + max_rows)
            return result
