from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_sqlite_write_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = weakref.WeakKeyDictionary()


class ResourceSessionNotIdle(RuntimeError):
    code = "RESOURCE_SESSION_NOT_IDLE"


def require_idle_resource_session(session: AsyncSession) -> None:
    """Reject a caller-owned transaction without modifying session state."""
    if session.in_transaction():
        raise ResourceSessionNotIdle(
            "resource mutation requires an idle session; "
            "commit or roll back the prior transaction first"
        )


@asynccontextmanager
async def serialized_resource_write(session: AsyncSession):
    """Serialize a complete resource mutation transaction.

    Callers must explicitly finish any earlier read transaction.  On SQLite,
    the process lock and ``BEGIN IMMEDIATE`` cover the mutation read through
    its commit or rollback; the database transaction remains the cross-process
    authority.
    """
    require_idle_resource_session(session)

    if session.get_bind().dialect.name != "sqlite":
        try:
            yield
        except BaseException:
            if session.in_transaction():
                await session.rollback()
            raise
        return

    loop = asyncio.get_running_loop()
    lock = _sqlite_write_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _sqlite_write_locks[loop] = lock
    async with cast(asyncio.Lock, lock):
        await session.execute(text("BEGIN IMMEDIATE"))
        try:
            yield
        except BaseException:
            if session.in_transaction():
                await session.rollback()
            raise
