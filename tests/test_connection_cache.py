import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.config import ConnectionPoolConfig, reset_config
from server.core.sql_executor import SQLExecutionError


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


def _make_mock_conn(*, ping_ok=True, database="testdb"):
    conn = AsyncMock()
    conn.ping = AsyncMock(side_effect=None if ping_ok else Exception("gone"))
    conn.close = MagicMock()
    conn.cursor = MagicMock()
    cursor_mock = AsyncMock()
    conn.cursor.return_value.__aenter__ = AsyncMock(return_value=cursor_mock)
    conn.cursor.return_value.__aexit__ = AsyncMock(return_value=False)
    # Expose the cursor mock so tests can assert on cur.execute(...) calls
    conn.cursor_mock = cursor_mock
    return conn


@pytest.fixture
def config():
    return ConnectionPoolConfig(
        idle_timeout_seconds=60,
        max_total_pools=3,
        health_check=True,
        cleanup_interval_s=1,
    )


class TestAcquire:
    async def test_creates_new_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire(
                user_id="u1", instance_id="i1",
                host="127.0.0.1", port=3306, user="dbuser", password="pw", database="testdb",
            )

        assert conn is mock_conn
        assert cache.size == 1

    async def test_reuses_cached_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn) as connect:
            conn1 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn1)
            conn2 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")

        assert conn1 is conn2
        assert connect.call_count == 1

    async def test_database_switch_issues_use(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "db1")
            await cache.release("u1", "i1", conn)
            await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "db2")

        mock_conn.cursor_mock.execute.assert_any_await("USE `db2`")

    async def test_database_none_reconnects_after_cached_database(self, config):
        from server.core.connection_cache import ConnectionCache

        db_conn = _make_mock_conn()
        no_db_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[db_conn, no_db_conn],
        ) as connect:
            conn1 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "db1")
            await cache.release("u1", "i1", conn1)
            conn2 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", None)

        assert conn2 is no_db_conn
        assert connect.call_count == 2
        assert connect.await_args.kwargs["db"] is None
        db_conn.close.assert_called_once()

    async def test_connection_identity_change_reconnects(self, config):
        from server.core.connection_cache import ConnectionCache

        old_conn = _make_mock_conn()
        new_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[old_conn, new_conn],
        ) as connect:
            conn1 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser1", "pw", "db1")
            await cache.release("u1", "i1", conn1)
            conn2 = await cache.acquire("u1", "i1", "127.0.0.2", 3307, "dbuser2", "pw", "db1")

        assert conn2 is new_conn
        assert connect.call_count == 2
        assert connect.await_args.kwargs["host"] == "127.0.0.2"
        assert connect.await_args.kwargs["port"] == 3307
        assert connect.await_args.kwargs["user"] == "dbuser2"
        old_conn.close.assert_called_once()

    async def test_password_change_reconnects(self, config):
        from server.core.connection_cache import ConnectionCache

        old_conn = _make_mock_conn()
        new_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[old_conn, new_conn],
        ) as connect:
            conn1 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw1", "db1")
            await cache.release("u1", "i1", conn1)
            conn2 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw2", "db1")

        assert conn2 is new_conn
        assert connect.call_count == 2
        assert connect.await_args.kwargs["password"] == "pw2"
        old_conn.close.assert_called_once()

    async def test_database_with_backtick_escaped(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "db1")
            await cache.release("u1", "i1", conn)
            await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "agentic@t`est")

        mock_conn.cursor_mock.execute.assert_any_await("USE `agentic@t``est`")

    async def test_database_switch_failure_discards_cached_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "db1")
            await cache.release("u1", "i1", conn)
            mock_conn.cursor_mock.execute = AsyncMock(side_effect=RuntimeError("unknown database"))
            with pytest.raises(RuntimeError, match="unknown database"):
                await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "db2")

        assert cache.size == 0
        mock_conn.close.assert_called_once()

    async def test_ping_failure_reconnects(self, config):
        from server.core.connection_cache import ConnectionCache

        dead_conn = _make_mock_conn(ping_ok=False)
        new_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        call_count = 0
        async def connect_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return dead_conn if call_count == 1 else new_conn

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, side_effect=connect_side_effect):
            conn1 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn1)
            conn2 = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")

        assert conn2 is new_conn
        dead_conn.close.assert_called_once()

    async def test_ping_failure_reconnect_failure_discards_cached_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        dead_conn = _make_mock_conn(ping_ok=False)
        cache = ConnectionCache(config)

        call_count = 0

        async def connect_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return dead_conn
            raise RuntimeError("connect failed")

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=connect_side_effect,
        ):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn)
            with pytest.raises(RuntimeError, match="connect failed"):
                await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")

        assert cache.size == 0
        dead_conn.close.assert_called_once()

    async def test_max_connections_rejects(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(config)  # max_total_pools=3

        conns = []
        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, side_effect=lambda **kw: _make_mock_conn()):
            for i in range(3):
                c = await cache.acquire(f"u{i}", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
                conns.append(c)

            with pytest.raises(SQLExecutionError, match="Connection limit reached"):
                await cache.acquire("u99", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")

    async def test_existing_key_not_blocked_by_max(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(config)  # max_total_pools=3

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, side_effect=lambda **kw: _make_mock_conn()):
            for i in range(3):
                c = await cache.acquire(f"u{i}", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
                await cache.release(f"u{i}", "i1", c)

            conn = await cache.acquire("u0", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            assert conn is not None

    async def test_existing_leased_key_is_blocked_by_max(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(ConnectionPoolConfig(
            idle_timeout_seconds=60,
            max_total_pools=1,
            health_check=True,
            cleanup_interval_s=1,
        ))
        conn1 = _make_mock_conn()
        conn2 = _make_mock_conn()

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[conn1, conn2],
        ) as connect:
            leased = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            with pytest.raises(SQLExecutionError, match="Connection limit reached"):
                await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", leased)

        assert connect.await_count == 1
        assert conn2.close.call_count == 0

    async def test_concurrent_acquire_different_keys_respects_max_connections(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(ConnectionPoolConfig(
            idle_timeout_seconds=60,
            max_total_pools=1,
            health_check=True,
            cleanup_interval_s=1,
        ))
        connect_count = 0

        async def slow_connect(**kwargs):
            nonlocal connect_count
            connect_count += 1
            await asyncio.sleep(0.05)
            return _make_mock_conn()

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, side_effect=slow_connect):
            results = await asyncio.gather(
                cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb"),
                cache.acquire("u2", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb"),
                return_exceptions=True,
            )

        successes = [result for result in results if not isinstance(result, Exception)]
        errors = [result for result in results if isinstance(result, SQLExecutionError)]
        assert len(successes) == 1
        assert len(errors) == 1
        assert errors[0].code == "CONNECTION_LIMIT"
        assert connect_count == 1
        await cache.release("u1" if successes[0] is results[0] else "u2", "i1", successes[0])

    async def test_concurrent_acquire_does_not_share_leased_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(config)
        connect_count = 0

        async def slow_connect(**kwargs):
            nonlocal connect_count
            connect_count += 1
            await asyncio.sleep(0.05)
            return _make_mock_conn()

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, side_effect=slow_connect):
            results = await asyncio.gather(
                cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb"),
                cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb"),
            )

        assert connect_count == 2
        assert results[0] is not results[1]

    async def test_reuses_connection_after_release_not_while_leased(self, config):
        from server.core.connection_cache import ConnectionCache

        conn1 = _make_mock_conn()
        conn2 = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[conn1, conn2],
        ) as connect:
            leased = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            concurrent = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", leased)
            await cache.release("u1", "i1", concurrent)
            reused = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")

        assert leased is conn1
        assert concurrent is conn2
        assert reused is conn2
        assert connect.call_count == 2


class TestRelease:
    async def test_normal_release_keeps_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn)

        assert cache.size == 1
        mock_conn.cursor_mock.execute.assert_any_await("ROLLBACK")
        mock_conn.cursor_mock.execute.assert_any_await("SET SESSION TRANSACTION READ WRITE")
        mock_conn.cursor_mock.execute.assert_any_await("SET autocommit=1")

    async def test_discard_release_removes_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn, discard=True)

        assert cache.size == 0
        mock_conn.close.assert_called_once()

    async def test_discard_release_closes_same_key_idle_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        conn1 = _make_mock_conn()
        conn2 = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[conn1, conn2],
        ):
            leased = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            concurrent = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", leased)
            await cache.release("u1", "i1", concurrent, discard=True)

        assert cache.size == 0
        conn1.close.assert_called_once()
        conn2.close.assert_called_once()

    async def test_release_cleanup_failure_discards(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        mock_conn.cursor_mock.execute = AsyncMock(side_effect=Exception("connection lost"))
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn)

        assert cache.size == 0
        mock_conn.close.assert_called_once()

    async def test_release_cleanup_failure_closes_replaced_idle_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        conn1 = _make_mock_conn()
        conn2 = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[conn1, conn2],
        ):
            leased = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            concurrent = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", leased)
            concurrent.cursor_mock.execute = AsyncMock(side_effect=Exception("connection lost"))
            await cache.release("u1", "i1", concurrent)

        assert cache.size == 0
        conn1.close.assert_called_once()
        conn2.close.assert_called_once()

    async def test_release_untracked_connection_closes_without_caching(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        await cache.release("u1", "i1", mock_conn)

        assert cache.size == 0
        mock_conn.close.assert_called_once()
        mock_conn.cursor_mock.execute.assert_not_awaited()

    async def test_double_release_removes_idle_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(config)

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn)
            await cache.release("u1", "i1", conn)

        assert cache.size == 0
        assert mock_conn.close.call_count == 1


class TestCleanup:
    async def test_idle_connections_cleaned(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(ConnectionPoolConfig(
            idle_timeout_seconds=0,
            max_total_pools=200,
            health_check=True,
            cleanup_interval_s=1,
        ))

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.release("u1", "i1", conn)

        await cache.cleanup_once()
        assert cache.size == 0
        mock_conn.close.assert_called_once()

    async def test_cleanup_keeps_lock_for_leased_connection(self, config):
        from server.core.connection_cache import ConnectionCache

        mock_conn = _make_mock_conn()
        cache = ConnectionCache(ConnectionPoolConfig(
            idle_timeout_seconds=0,
            max_total_pools=200,
            health_check=True,
            cleanup_interval_s=1,
        ))

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, return_value=mock_conn):
            conn = await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.cleanup_once()

        assert ("u1", "i1") in cache._locks
        assert cache.size == 1
        mock_conn.close.assert_not_called()
        await cache.release("u1", "i1", conn)

    async def test_close_all(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(config)
        conns = []

        with patch("server.core.connection_cache.asyncmy.connect", new_callable=AsyncMock, side_effect=lambda **kw: _make_mock_conn()):
            for i in range(3):
                c = await cache.acquire(f"u{i}", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
                await cache.release(f"u{i}", "i1", c)
                conns.append(c)

        await cache.close_all()
        assert cache.size == 0
        for c in conns:
            c.close.assert_called_once()

    async def test_close_all_closes_leased_connections(self, config):
        from server.core.connection_cache import ConnectionCache

        cache = ConnectionCache(config)
        conn1 = _make_mock_conn()
        conn2 = _make_mock_conn()

        with patch(
            "server.core.connection_cache.asyncmy.connect",
            new_callable=AsyncMock,
            side_effect=[conn1, conn2],
        ):
            await cache.acquire("u1", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")
            await cache.acquire("u2", "i1", "127.0.0.1", 3306, "dbuser", "pw", "testdb")

        await cache.close_all()

        assert cache.size == 0
        conn1.close.assert_called_once()
        conn2.close.assert_called_once()
