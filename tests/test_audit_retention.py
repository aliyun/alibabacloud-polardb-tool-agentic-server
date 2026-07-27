from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import AuditConfig
from server.core.audit_retention import (
    audit_retention_loop,
    sweep_expired_audit_logs,
)
from server.models import AuditLog, AuditStatus, Base, User
from server.models.base import utc_now


@pytest.fixture
async def retention_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user = User(external_id="retention-user", display_name="Retention")
        session.add(user)
        await session.flush()
        now = utc_now()
        for days in (220, 200, 181, 179):
            session.add(
                AuditLog(
                    actor_user_id=user.id,
                    action=f"retention.{days}",
                    target_type="test",
                    status=AuditStatus.SUCCESS,
                    created_at=now - timedelta(days=days),
                )
            )
        await session.commit()
    yield factory, now
    await engine.dispose()


async def test_sweep_deletes_oldest_expired_rows_in_bounded_batches(
    retention_db,
):
    factory, now = retention_db

    deleted = await sweep_expired_audit_logs(
        factory,
        retention_days=180,
        batch_size=2,
        now=now,
    )

    assert deleted == 2
    async with factory() as session:
        actions = (
            await session.execute(
                select(AuditLog.action).order_by(AuditLog.created_at)
            )
        ).scalars().all()
    assert actions == ["retention.181", "retention.179"]

    assert (
        await sweep_expired_audit_logs(
            factory,
            retention_days=180,
            batch_size=2,
            now=now,
        )
        == 1
    )
    assert (
        await sweep_expired_audit_logs(
            factory,
            retention_days=180,
            batch_size=2,
            now=now,
        )
        == 0
    )


def test_audit_retention_defaults_are_safe_and_bounded():
    config = AuditConfig()
    assert config.retention_days == 180
    assert config.cleanup_interval_seconds == 3600
    assert config.cleanup_batch_size == 500


async def test_audit_retention_loop_cancels_cleanly():
    async def unused_factory():
        raise AssertionError("sweep must not run before the interval")

    task = asyncio.create_task(
        audit_retention_loop(
            unused_factory,  # type: ignore[arg-type]
            AuditConfig(cleanup_interval_seconds=3600),
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
