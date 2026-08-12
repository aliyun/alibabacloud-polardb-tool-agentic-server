from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.core.provisioning_operation_budget import (
    RateLimited,
    consume_hourly_budget,
)
from server.core.dedicated_pool_repository import (
    consume_agent_operation_budget,
)
from server.models import Base, ProvisioningOperationBudget


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


async def test_hourly_budget_rejects_overspend_and_rolls_over(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    for _ in range(2):
        await consume_hourly_budget(
            session,
            scope_type="pool",
            scope_id="pool-1",
            operation="purchase",
            limit=2,
            now=now,
        )
        await session.commit()

    with pytest.raises(RateLimited):
        await consume_hourly_budget(
            session,
            scope_type="pool",
            scope_id="pool-1",
            operation="purchase",
            limit=2,
            now=now,
        )
    await session.rollback()

    await consume_hourly_budget(
        session,
        scope_type="pool",
        scope_id="pool-1",
        operation="purchase",
        limit=2,
        now=now + timedelta(hours=1),
    )
    await session.commit()
    rows = (await session.execute(
        ProvisioningOperationBudget.__table__.select()
    )).all()
    assert sorted(row.request_count for row in rows) == [1, 2]


async def test_budget_limit_is_shared_across_independent_sessions(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    await consume_hourly_budget(
        session,
        scope_type="pool_agent",
        scope_id="pool-1:agent-1",
        operation="create",
        limit=1,
        now=now,
    )
    await session.commit()

    async with AsyncSession(session.bind, expire_on_commit=False) as contender:
        with pytest.raises(RateLimited):
            await consume_hourly_budget(
                contender,
                scope_type="pool_agent",
                scope_id="pool-1:agent-1",
                operation="create",
                limit=1,
                now=now,
            )


async def test_agent_create_and_delete_budgets_are_independent(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    pool = SimpleNamespace(
        id="pool-separate",
        max_create_requests_per_agent_per_hour=1,
        max_delete_requests_per_agent_per_hour=1,
    )
    await consume_agent_operation_budget(
        session,
        pool=pool,
        agent_id="agent-1",
        operation="create",
        now=now,
    )
    await consume_agent_operation_budget(
        session,
        pool=pool,
        agent_id="agent-1",
        operation="delete",
        now=now,
    )
    with pytest.raises(RateLimited):
        await consume_agent_operation_budget(
            session,
            pool=pool,
            agent_id="agent-1",
            operation="create",
            now=now,
        )
