from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3
from typing import TypeVar, overload
import weakref

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from server.bootstrap import load_bootstrap_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_CursorT = TypeVar("_CursorT", bound=sqlite3.Cursor)


class _SQLiteConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cursors: weakref.WeakSet[sqlite3.Cursor] = weakref.WeakSet()

    @overload
    def cursor(self, factory: None = None) -> sqlite3.Cursor: ...

    @overload
    def cursor(self, factory: Callable[[sqlite3.Connection], _CursorT]) -> _CursorT: ...

    def cursor(self, factory: Callable[[sqlite3.Connection], sqlite3.Cursor] | None = None) -> sqlite3.Cursor:
        cursor = super().cursor() if factory is None else super().cursor(factory)
        self._cursors.add(cursor)
        return cursor

    def close(self) -> None:
        # A failed or cancelled execute can retain its cursor in a traceback.
        # Finalize it before close_v2 can defer rollback and leave a zombie DB.
        try:
            for cursor in self._cursors:
                cursor.close()
            self._cursors.clear()
        finally:
            super().close()


def sqlite_connect_args(url: str) -> dict:
    """Apply cursor cleanup to all PAS SQLite engines, including NullPool users."""
    return {"factory": _SQLiteConnection} if make_url(url).get_backend_name() == "sqlite" else {}


def enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Enable SQLite foreign keys and roll back transactions during pool reset."""
    if not engine.url.drivername.startswith("sqlite"):
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "reset")
    def _rollback_failed_commit(dbapi_connection, _connection_record, reset_state) -> None:
        # A failed COMMIT deactivates SQLAlchemy's transaction without rolling
        # SQLite back. Connection.close() can then incorrectly skip pool reset.
        # Rolling back an already reset SQLite transaction is a no-op.
        if reset_state.asyncio_safe and reset_state.transaction_was_reset:
            dbapi_connection.rollback()


def _ensure_sqlite_parent_dir(url: str) -> None:
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return
    if not parsed.database or parsed.database == ":memory:":
        return
    Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = load_bootstrap_settings().database_url
        _ensure_sqlite_parent_dir(url)
        kwargs: dict = {"connect_args": sqlite_connect_args(url)}
        if make_url(url).get_backend_name() != "sqlite":
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 10
        _engine = create_async_engine(url, echo=False, **kwargs)
        enable_sqlite_foreign_keys(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session():
    """FastAPI dependency for DB sessions."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def reset_engine() -> None:
    """Reset engine for testing."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
