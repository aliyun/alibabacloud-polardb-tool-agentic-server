import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.config import reset_config
from server.core.sql_executor import SQLExecutionError


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


def _make_mock_cursor(*, rows=None, columns=None):
    cursor = AsyncMock()
    cursor.description = [(c,) for c in (columns or [])] if columns else None
    cursor.fetchall = AsyncMock(return_value=rows or [])
    cursor.execute = AsyncMock()
    return cursor


def _make_mock_cache():
    cache = AsyncMock()
    return cache


def _make_mock_conn(cursor):
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=cursor)
    ctx.__aexit__ = AsyncMock(return_value=False)
    conn.cursor = MagicMock(return_value=ctx)
    return conn


def test_mentions_session_branch_variants():
    from server.core.sql_gateway import _mentions_session_branch

    assert _mentions_session_branch("SELECT @@session.branch")
    assert _mentions_session_branch("SELECT @@SESSION.`branch`")
    assert _mentions_session_branch("SET @@session.branch = 'br1'")
    assert _mentions_session_branch("set @@LOCAL.`branch` = 'br1'")
    assert _mentions_session_branch("SET @@branch = 'br1'")
    assert _mentions_session_branch("SET SESSION branch = 'br1'")
    assert _mentions_session_branch("SET LOCAL `branch` = 'br1'")
    assert _mentions_session_branch("SET branch = 'br1'")
    assert _mentions_session_branch("SET @@session./**/branch = 'br1'")
    assert _mentions_session_branch("SET @@session.-- comment\nbranch = 'br1'")
    assert _mentions_session_branch("SET @@session.# comment\nbranch = 'br1'")
    assert _mentions_session_branch("/*! SET @@session.branch = 'br1' */ SELECT 1")
    assert _mentions_session_branch("/*!50000 SET @@session.branch = 'br1' */ SELECT 1")
    assert _mentions_session_branch("SELECT '-- comment'; SET @@session.branch = 'br1'")
    assert _mentions_session_branch("SELECT '/* comment */'; SET @@session.branch = 'br1'")
    assert not _mentions_session_branch("SELECT branch_name FROM branches")
    assert not _mentions_session_branch("SELECT '@@session.branch'")
    assert not _mentions_session_branch('SELECT "SET SESSION branch = br1"')
    assert not _mentions_session_branch("SELECT '/*!50000 SET @@session.branch = br1 */'")
    assert not _mentions_session_branch("/* SET @@session.branch = 'br1' */ SELECT 1")


def test_changes_database_context_variants():
    from server.core.sql_gateway import _changes_database_context

    assert _changes_database_context("USE db1")
    assert _changes_database_context("use `db1`")
    assert _changes_database_context("/* comment */ USE db1")
    assert _changes_database_context("/*! USE db1 */ SELECT 1")
    assert _changes_database_context("/*!50000 USE db1 */ SELECT 1")
    assert _changes_database_context("SELECT 1; USE db1")
    assert _changes_database_context("SELECT 'USE db1'; USE db2")
    assert not _changes_database_context("SELECT 'USE db1'")
    assert not _changes_database_context("SELECT '/*!50000 USE db1 */'")
    assert not _changes_database_context("SELECT 1; SELECT 'USE db1'")
    assert not _changes_database_context("SELECT * FROM usage_stats")
    assert not _changes_database_context("/* USE db1 */ SELECT 1")
    assert not _changes_database_context("/* !50000 USE db1 */ SELECT 1")


def test_json_safe_cell_binary_fallback():
    from server.core.sql_gateway import _json_safe_cell

    assert _json_safe_cell(b"\xff\xfe") == "0xfffe"
    assert _json_safe_cell(bytearray(b"abc")) == "abc"


class TestExecute:
    async def test_generic_connection_error_is_sanitized(self):
        from server.core.sql_gateway import SQLGateway

        sentinel = "password=SECRET host=private"
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(side_effect=RuntimeError(sentinel))

        with pytest.raises(SQLExecutionError) as error:
            await SQLGateway(cache).execute(
                host="private",
                port=3306,
                user="u",
                password="SECRET",
                sql="SELECT 1",
                database="secret_db",
                user_id="uid",
                instance_id="iid",
            )
        assert error.value.code == "CONNECTION_ERROR"
        assert error.value.message == "Database connection failed"
        assert sentinel not in str(error.value)

    async def test_generic_driver_error_is_sanitized(self):
        from server.core.sql_gateway import SQLGateway

        sentinel = "password=SECRET host=private SQL=SELECT sentinel"
        cursor = _make_mock_cursor()
        cursor.execute = AsyncMock(side_effect=RuntimeError(sentinel))
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        with pytest.raises(SQLExecutionError) as error:
            await SQLGateway(cache).execute(
                host="private",
                port=3306,
                user="u",
                password="SECRET",
                sql="SELECT sentinel",
                database="secret_db",
                user_id="uid",
                instance_id="iid",
            )
        assert error.value.code == "SQL_ERROR"
        assert error.value.message == "Database operation failed"
        assert sentinel not in str(error.value)

    async def test_single_select(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["id", "name"], rows=[(1, "alice"), (2, "bob")])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        result = await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT * FROM users",
            database="db", user_id="uid", instance_id="iid",
        )

        assert result["columns"] == ["id", "name"]
        assert result["rows"] == [[1, "alice"], [2, "bob"]]
        assert result["row_count"] == 2
        assert result["truncated"] is False
        cache.release.assert_awaited_once()
        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is False

    async def test_driver_values_are_json_safe(self):
        import json
        from datetime import date, datetime, time, timedelta
        from decimal import Decimal

        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(
            columns=["ts", "d", "t", "tm", "dec", "blob", "n"],
            rows=[(
                datetime(2026, 1, 2, 3, 4, 5),
                date(2026, 1, 2),
                time(3, 4, 5),
                timedelta(hours=10, minutes=1),
                Decimal("1.10"),
                b"hello",
                None,
            )],
        )
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        result = await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid",
        )

        assert result["rows"] == [[
            "2026-01-02 03:04:05",
            "2026-01-02",
            "03:04:05",
            "10:01:00",
            "1.10",
            "hello",
            None,
        ]]
        json.dumps(result)

    async def test_read_only_sets_read_write_var(self):
        """Gateway always sets READ WRITE — read-only enforcement is at app level."""
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid", read_only=True,
        )

        cursor.execute.assert_any_await("SET SESSION TRANSACTION READ WRITE")

    async def test_execute_does_not_touch_branch_when_omitted(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid",
        )

        branch_calls = [c for c in cursor.execute.call_args_list if "@@session.branch" in str(c)]
        assert branch_calls == []

    async def test_execute_discards_when_user_sql_touches_session_branch(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p",
            sql="SET @@session.branch = 'br1'",
            database="db", user_id="uid", instance_id="iid",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_execute_discards_when_user_sql_changes_database_context(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p",
            sql="USE db1",
            database=None, user_id="uid", instance_id="iid",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_execute_discards_when_later_statement_changes_database_context(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p",
            sql="SELECT 1; USE db1",
            database=None, user_id="uid", instance_id="iid",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_execute_keeps_connection_when_use_only_appears_in_string(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[("USE db1",)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p",
            sql="SELECT 'USE db1'",
            database=None, user_id="uid", instance_id="iid",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is False

    async def test_execute_sets_requested_branch(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid", branch="br1",
        )

        cursor.execute.assert_any_await("SET @@session.branch = %s", ("br1",))
        cursor.execute.assert_any_await("SET @@session.branch = ''")

    async def test_execute_empty_branch_forces_default_branch(self):
        """Branch tools pass an empty branch to clear stale session branch state."""
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["Branch"], rows=[("MAIN",)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SHOW BRANCHES",
            database="db", user_id="uid", instance_id="iid", branch="",
        )

        sql_calls = [call.args[0] for call in cursor.execute.call_args_list]
        assert sql_calls.index("SET @@session.branch = %s") < sql_calls.index("SHOW BRANCHES")
        cursor.execute.assert_any_await("SET @@session.branch = %s", ("",))
        cursor.execute.assert_any_await("SET @@session.branch = ''")
        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is False

    async def test_execute_sets_branch_before_user_sql_and_restores_after(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid", branch="br1",
        )

        sql_calls = [call.args[0] for call in cursor.execute.call_args_list]
        assert sql_calls.index("SET @@session.branch = %s") < sql_calls.index(
            "SELECT 1 LIMIT 1001 OFFSET 0",
        )
        assert sql_calls.index("SELECT 1 LIMIT 1001 OFFSET 0") < sql_calls.index(
            "SET @@session.branch = ''",
        )

    async def test_execute_discards_when_user_sql_changes_branch_with_requested_branch(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p",
            sql="SET @@session.branch = 'br2'",
            database="db", user_id="uid", instance_id="iid", branch="br1",
        )

        cursor.execute.assert_any_await("SET @@session.branch = %s", ("br1",))
        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_execute_parameterizes_requested_branch_literal(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid", branch="br'\\x",
        )

        cursor.execute.assert_any_await("SET @@session.branch = %s", ("br'\\x",))

    async def test_execute_discards_connection_when_branch_restore_fails(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])

        async def execute_side_effect(sql, *args):
            if sql == "SET @@session.branch = ''":
                raise RuntimeError("restore failed")

        cursor.execute = AsyncMock(side_effect=execute_side_effect)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid", branch="br1",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_execute_discards_connection_when_branch_set_fails(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])

        async def execute_side_effect(sql, *args):
            if sql == "SET @@session.branch = %s":
                raise RuntimeError("branch set failed")

        cursor.execute = AsyncMock(side_effect=execute_side_effect)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        with pytest.raises(
            SQLExecutionError, match="Database operation failed"
        ):
            await gw.execute(
                host="h", port=3306, user="u", password="p", sql="SELECT 1",
                database="db", user_id="uid", instance_id="iid", branch="br1",
            )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_select_sets_query_timeout(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT 1",
            database="db", user_id="uid", instance_id="iid",
        )

        timeout_calls = [c for c in cursor.execute.call_args_list if "max_execution_time" in str(c)]
        assert len(timeout_calls) == 1

    async def test_insert_no_query_timeout(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute(
            host="h", port=3306, user="u", password="p",
            sql="INSERT INTO t VALUES (1)", database="db",
            user_id="uid", instance_id="iid",
        )

        timeout_calls = [c for c in cursor.execute.call_args_list if "max_execution_time" in str(c)]
        assert len(timeout_calls) == 0

    async def test_timeout_discards_connection(self):
        from server.core.sql_gateway import SQLGateway

        async def slow_execute(sql):
            if sql.startswith("SET "):
                return None
            await asyncio.sleep(999)

        cursor = _make_mock_cursor(columns=["x"], rows=[(1,)])
        cursor.execute = AsyncMock(side_effect=slow_execute)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        with patch("server.core.sql_gateway.get_config") as mock_cfg:
            mock_cfg.return_value.sql_security.max_timeout_ms = 100
            mock_cfg.return_value.sql_security.timeout_ms = 100
            with pytest.raises(SQLExecutionError, match="timed out"):
                await gw.execute(
                    host="h", port=3306, user="u", password="p", sql="SELECT 1",
                    database="db", user_id="uid", instance_id="iid",
                )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_truncation(self):
        from server.core.sql_gateway import SQLGateway

        rows = [(i,) for i in range(1002)]
        cursor = _make_mock_cursor(columns=["id"], rows=rows)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        result = await gw.execute(
            host="h", port=3306, user="u", password="p", sql="SELECT id FROM t",
            database="db", user_id="uid", instance_id="iid", max_rows=1000,
        )

        assert result["truncated"] is True
        assert len(result["rows"]) == 1000
        assert "next_cursor" in result


class TestExecuteTransaction:
    async def test_generic_connection_error_is_sanitized(self):
        from server.core.sql_gateway import SQLGateway

        sentinel = "password=SECRET host=private"
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(side_effect=RuntimeError(sentinel))

        with pytest.raises(SQLExecutionError) as error:
            await SQLGateway(cache).execute_transaction(
                host="private",
                port=3306,
                user="u",
                password="SECRET",
                sql_statements=["INSERT INTO t VALUES (1)"],
                database="secret_db",
                user_id="uid",
                instance_id="iid",
            )
        assert error.value.code == "CONNECTION_ERROR"
        assert error.value.message == "Database connection failed"
        assert sentinel not in str(error.value)

    async def test_generic_driver_error_is_sanitized(self):
        from server.core.sql_gateway import SQLGateway

        sentinel = "password=SECRET host=private SQL=INSERT sentinel"
        cursor = _make_mock_cursor()
        cursor.execute = AsyncMock(side_effect=RuntimeError(sentinel))
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        with pytest.raises(SQLExecutionError) as error:
            await SQLGateway(cache).execute_transaction(
                host="private",
                port=3306,
                user="u",
                password="SECRET",
                sql_statements=["INSERT sentinel"],
                database="secret_db",
                user_id="uid",
                instance_id="iid",
            )
        assert error.value.code == "SQL_ERROR"
        assert error.value.message == "Database operation failed"
        assert sentinel not in str(error.value)

    async def test_begin_commit_flow(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        results = await gw.execute_transaction(
            host="h", port=3306, user="u", password="p",
            sql_statements=["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"],
            database="db", user_id="uid", instance_id="iid",
        )

        assert len(results) == 2
        cursor.execute.assert_any_await("BEGIN")
        cursor.execute.assert_any_await("COMMIT")

    async def test_mid_failure_rollback(self):
        from server.core.sql_gateway import SQLGateway

        user_call_count = 0

        async def execute_side_effect(sql):
            nonlocal user_call_count
            # Let internal commands pass through (BEGIN, COMMIT, ROLLBACK, SET)
            if sql in ("BEGIN", "COMMIT", "ROLLBACK") or sql.startswith("SET "):
                return None
            user_call_count += 1
            if user_call_count == 2:
                raise Exception("syntax error")

        cursor = _make_mock_cursor()
        cursor.execute = AsyncMock(side_effect=execute_side_effect)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        with pytest.raises(
            SQLExecutionError, match="Database operation failed"
        ):
            await gw.execute_transaction(
                host="h", port=3306, user="u", password="p",
                sql_statements=["INSERT INTO t VALUES (1)", "INVALID SQL"],
                database="db", user_id="uid", instance_id="iid",
            )

        cursor.execute.assert_any_await("ROLLBACK")

    async def test_transaction_does_not_touch_branch_state(self):
        """Transaction keeps branch optionality isolated to run_sql."""
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute_transaction(
            host="h", port=3306, user="u", password="p",
            sql_statements=["SELECT 1"],
            database="db", user_id="uid", instance_id="iid", read_only=True,
        )

        cursor.execute.assert_any_await("SET SESSION TRANSACTION READ WRITE")
        branch_calls = [c for c in cursor.execute.call_args_list if "@@session.branch" in str(c)]
        assert branch_calls == []

    async def test_transaction_discards_when_user_sql_touches_session_branch(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute_transaction(
            host="h", port=3306, user="u", password="p",
            sql_statements=["SET @@session.branch = 'br1'", "SELECT 1"],
            database="db", user_id="uid", instance_id="iid",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_transaction_discards_when_user_sql_changes_database_context(self):
        from server.core.sql_gateway import SQLGateway

        cursor = _make_mock_cursor()
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        await gw.execute_transaction(
            host="h", port=3306, user="u", password="p",
            sql_statements=["USE db1", "SELECT 1"],
            database=None, user_id="uid", instance_id="iid",
        )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_transaction_timeout(self):
        from server.core.sql_gateway import SQLGateway

        async def slow_execute(sql):
            # Let internal commands pass through (BEGIN, COMMIT, ROLLBACK, SET)
            if sql in ("BEGIN", "COMMIT", "ROLLBACK") or sql.startswith("SET "):
                return None
            await asyncio.sleep(999)

        cursor = _make_mock_cursor()
        cursor.execute = AsyncMock(side_effect=slow_execute)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        with patch("server.core.sql_gateway.get_config") as mock_cfg:
            mock_cfg.return_value.sql_security.max_timeout_ms = 100
            with pytest.raises(SQLExecutionError, match="timed out"):
                await gw.execute_transaction(
                    host="h", port=3306, user="u", password="p",
                    sql_statements=["SELECT SLEEP(100)"],
                    database="db", user_id="uid", instance_id="iid",
                )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is True

    async def test_rollback_success_keeps_connection(self):
        from server.core.sql_gateway import SQLGateway

        user_call_count = 0

        async def execute_side_effect(sql):
            nonlocal user_call_count
            # Let internal commands pass through (BEGIN, COMMIT, ROLLBACK, SET)
            if sql in ("BEGIN", "COMMIT", "ROLLBACK") or sql.startswith("SET "):
                return None
            user_call_count += 1
            if user_call_count == 2:
                raise Exception("error")

        cursor = _make_mock_cursor()
        cursor.execute = AsyncMock(side_effect=execute_side_effect)
        conn = _make_mock_conn(cursor)
        cache = _make_mock_cache()
        cache.acquire = AsyncMock(return_value=conn)

        gw = SQLGateway(cache)
        with pytest.raises(
            SQLExecutionError, match="Database operation failed"
        ):
            await gw.execute_transaction(
                host="h", port=3306, user="u", password="p",
                sql_statements=["INSERT INTO t VALUES (1)", "BAD SQL"],
                database="db", user_id="uid", instance_id="iid",
            )

        _, kwargs = cache.release.call_args
        assert kwargs.get("discard") is False
