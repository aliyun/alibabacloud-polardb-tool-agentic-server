from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from dataclasses import dataclass

import asyncmy  # type: ignore[import-untyped]

from server.config import ConnectionPoolConfig
from server.core.sql_executor import SQLExecutionError

logger = logging.getLogger(__name__)
_PASSWORD_FINGERPRINT_KEY = secrets.token_bytes(32)


async def _exec_simple(conn: asyncmy.Connection, sql: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(sql)


@dataclass
class CachedConnection:
    conn: asyncmy.Connection
    created_at: float
    last_used: float
    host: str
    port: int
    user: str
    password_fingerprint: str
    database: str | None


def _password_fingerprint(password: str) -> str:
    return hmac.digest(
        _PASSWORD_FINGERPRINT_KEY,
        password.encode("utf-8"),
        "sha256",
    ).hex()


class ConnectionCache:
    def __init__(self, config: ConnectionPoolConfig) -> None:
        self._config = config
        self._cache: dict[tuple[str, str], CachedConnection] = {}
        self._leased: dict[tuple[str, str, int], CachedConnection] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._capacity_lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._cache) + len(self._leased)

    def _get_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _ping(self, conn: asyncmy.Connection) -> bool:
        if not self._config.health_check:
            return True
        try:
            await conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    async def acquire(
        self,
        user_id: str,
        instance_id: str,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str | None,
    ) -> asyncmy.Connection:
        key = (user_id, instance_id)
        password_fingerprint = _password_fingerprint(password)

        async with self._get_lock(key):
            cached = self._cache.get(key)
            if cached and await self._ping(cached.conn):
                connection_identity_changed = (
                    cached.host != host
                    or cached.port != port
                    or cached.user != user
                    or cached.password_fingerprint != password_fingerprint
                )
                stale_database_context = database is None and cached.database is not None
                if connection_identity_changed or stale_database_context:
                    try:
                        cached.conn.close()
                    except Exception:
                        pass
                    self._cache.pop(key, None)
                    cached = None
                elif database and database != cached.database:
                    safe_db = database.replace("`", "``")
                    try:
                        await _exec_simple(cached.conn, f"USE `{safe_db}`")
                    except Exception:
                        try:
                            cached.conn.close()
                        except Exception:
                            pass
                        self._cache.pop(key, None)
                        raise
                    cached.database = database
                if cached:
                    self._cache.pop(key, None)
                    cached.last_used = time.time()
                    self._leased[(user_id, instance_id, id(cached.conn))] = cached
                    return cached.conn

            if cached:
                try:
                    cached.conn.close()
                except Exception:
                    pass
                self._cache.pop(key, None)

            async with self._capacity_lock:
                if self.size >= self._config.max_total_pools:
                    raise SQLExecutionError("Connection limit reached", "CONNECTION_LIMIT")

                try:
                    conn = await asyncmy.connect(
                        host=host, port=port, user=user, password=password,
                        db=database, connect_timeout=10, autocommit=True,
                    )
                except Exception:
                    self._cache.pop(key, None)
                    raise
                now = time.time()
                cached = CachedConnection(
                    conn=conn, created_at=now, last_used=now,
                    host=host, port=port, user=user,
                    password_fingerprint=password_fingerprint,
                    database=database,
                )
                self._leased[(user_id, instance_id, id(conn))] = cached
                return conn

    async def release(
        self,
        user_id: str,
        instance_id: str,
        conn: asyncmy.Connection,
        *,
        discard: bool = False,
    ) -> None:
        key = (user_id, instance_id)
        leased_key = (user_id, instance_id, id(conn))

        async with self._get_lock(key):
            cached = self._leased.pop(leased_key, None)
            if cached is None:
                idle = self._cache.get(key)
                if idle and idle.conn is conn:
                    self._cache.pop(key, None)
                try:
                    conn.close()
                except Exception:
                    pass
                return

            if discard:
                try:
                    conn.close()
                except Exception:
                    pass
                idle = self._cache.pop(key, None)
                if idle and idle.conn is not conn:
                    try:
                        idle.conn.close()
                    except Exception:
                        pass
                return

            try:
                await _exec_simple(conn, "ROLLBACK")
                # Avoid RESET CONNECTION: on PolarDB cluster endpoints (proxy),
                # it resets session routing state, causing the next SELECT to pin
                # the session to a read-only backend.  Instead, restore the two
                # session properties this cache layer touches.
                await _exec_simple(conn, "SET SESSION TRANSACTION READ WRITE")
                await _exec_simple(conn, "SET autocommit=1")
                cached.last_used = time.time()
                idle = self._cache.pop(key, None)
                if idle and idle.conn is not conn:
                    try:
                        idle.conn.close()
                    except Exception:
                        pass
                self._cache[key] = cached
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                idle = self._cache.pop(key, None)
                if idle and idle.conn is not conn:
                    try:
                        idle.conn.close()
                    except Exception:
                        pass

    async def cleanup_once(self) -> None:
        now = time.time()
        expired_keys = [
            k for k, v in self._cache.items()
            if now - v.last_used > self._config.idle_timeout_seconds
        ]
        for key in expired_keys:
            cached = self._cache.pop(key, None)
            if cached:
                try:
                    cached.conn.close()
                except Exception:
                    pass

        leased_keys = {
            (user_id, instance_id)
            for user_id, instance_id, _conn_id in self._leased
        }
        stale_locks = [
            k for k in self._locks
            if k not in self._cache and k not in leased_keys and not self._locks[k].locked()
        ]
        for k in stale_locks:
            del self._locks[k]

    async def run_cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.cleanup_interval_s)
                await self.cleanup_once()
        except asyncio.CancelledError:
            pass

    async def close_all(self) -> None:
        for key in list(self._cache.keys()):
            cached = self._cache.pop(key, None)
            if cached:
                try:
                    cached.conn.close()
                except Exception:
                    pass
        for leased_key in list(self._leased.keys()):
            cached = self._leased.pop(leased_key, None)
            if cached:
                try:
                    cached.conn.close()
                except Exception:
                    pass
        self._locks.clear()
