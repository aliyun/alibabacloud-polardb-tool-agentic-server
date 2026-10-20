import base64

import pytest
from sqlalchemy import text

from server.config import reset_config
from server.db.engine import get_engine, reset_engine


_KEY = base64.b64encode(b"01234567890123456789012345678901").decode()


def test_engine_starts_from_minimal_bootstrap(tmp_path, monkeypatch):
    db_path = tmp_path / "minimal.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()

    engine = get_engine()

    assert engine.url.database == str(db_path)
    assert engine.echo is False
    reset_engine()


def test_file_sqlite_url_creates_parent_directory(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "app.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()

    get_engine()

    assert db_path.parent.is_dir()
    reset_engine()


@pytest.mark.parametrize("driver", ["postgresql+asyncpg", "mysql+asyncmy"])
@pytest.mark.parametrize(
    "address",
    [
        "sqlite_user:test@127.0.0.1:1/archive",
        "test:sqlite_password@127.0.0.1:1/archive",
        "test:test@sqlite.example.invalid:1/archive",
        "test:test@127.0.0.1:1/sqlite_archive",
    ],
)
async def test_non_sqlite_url_does_not_receive_sqlite_factory(driver, address, monkeypatch):
    from sqlalchemy import event

    monkeypatch.setenv("PAS_DATABASE_URL", f"{driver}://{address}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()
    engine = get_engine()

    @event.listens_for(engine.sync_engine, "do_connect")
    def inspect_connection_parameters(_dialect, _record, _args, params):
        assert "factory" not in params
        # Stop before network I/O after inspecting the actual dialect arguments.
        raise RuntimeError("connection parameters inspected")

    try:
        with pytest.raises(RuntimeError, match="connection parameters inspected"):
            async with engine.connect():
                pass
        assert engine.pool.size() == 10
    finally:
        await engine.dispose()
        reset_engine()


async def test_default_sqlite_engine_enforces_foreign_keys(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "foreign-keys.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()

    engine = get_engine()
    async with engine.connect() as connection:
        enabled = await connection.scalar(text("PRAGMA foreign_keys"))

    assert enabled == 1
    await engine.dispose()
    reset_engine()


async def test_busy_commit_does_not_return_an_open_write_transaction(tmp_path, monkeypatch):
    import sqlite3

    import pytest
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    db_path = tmp_path / "busy-commit.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}?timeout=0.02")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()
    engine = get_engine()
    factory = async_sessionmaker(engine)
    returned_transactions = []

    @event.listens_for(engine.sync_engine, "checkin")
    def record_transaction(connection, _record):
        if connection is not None:
            returned_transactions.append(connection.driver_connection.in_transaction)

    reader = sqlite3.connect(db_path)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE committed_values (value INTEGER)"))
            await connection.execute(text("INSERT INTO committed_values VALUES (1)"))
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM committed_values").fetchall()
        with pytest.raises(OperationalError, match="database is locked"):
            async with factory() as session:
                await session.execute(text("UPDATE committed_values SET value=2"))
                await session.commit()
        reader.rollback()
        assert returned_transactions and not any(returned_transactions)
        async with factory() as session:
            assert await session.scalar(text("SELECT value FROM committed_values")) == 1
        reader.execute("INSERT INTO committed_values VALUES (3)")
        reader.commit()
    finally:
        reader.close()
        await engine.dispose()
        reset_engine()


async def test_cancelled_select_releases_cursor_and_uncommitted_write(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    import threading

    import pytest
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker

    db_path = tmp_path / "cancelled-select.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}?timeout=0.02")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()
    engine = get_engine()
    factory = async_sessionmaker(engine)
    entered = threading.Event()
    release = threading.Event()
    native_connections = []

    def pause(value):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test query was not released")
        return value

    @event.listens_for(engine.sync_engine, "connect")
    def register_pause(connection, _record):
        native_connections.append(connection.driver_connection._conn)
        connection.create_function("pause", 1, pause)

    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE committed_values (value INTEGER)"))
        await connection.execute(text("INSERT INTO committed_values VALUES (1), (2)"))

    async def interrupted_transaction():
        async with factory() as session:
            await session.execute(text("UPDATE committed_values SET value=value+10"))
            await session.execute(text("SELECT pause(value) FROM committed_values"))

    task = asyncio.create_task(interrupted_transaction())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as retained_error:
            await task
        # Keep the real cancellation traceback alive throughout the write check.
        assert retained_error.value.__traceback__ is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            native_connections[0].execute("SELECT 1")
        with sqlite3.connect(db_path, timeout=0.02) as writer:
            writer.execute("INSERT INTO committed_values VALUES (3)")
            writer.commit()
            assert writer.execute("SELECT value FROM committed_values ORDER BY value").fetchall() == [
                (1,), (2,), (3,),
            ]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()
        reset_engine()
