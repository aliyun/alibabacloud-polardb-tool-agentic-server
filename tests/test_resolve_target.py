from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.mcp.tools import resolve_target_instance
from server.models import (
    AllocationMode,
    AuthProvider,
    Base,
    Instance,
    InstanceStatus,
    InstanceTopology,
    User,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def _user(session, suffix: str) -> User:
    user = User(
        external_id=f"resolve-{suffix}",
        display_name="Resolve User",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.commit()
    return user


def _error_code(result: dict) -> str:
    return json.loads(result["content"][0]["text"])["error"]


async def test_no_assigned_instance_returns_admin_guidance(session):
    user = await _user(session, "empty")

    result = await resolve_target_instance(user, session)

    assert isinstance(result, dict)
    assert _error_code(result) == "NO_INSTANCE_ASSIGNED"


@pytest.mark.parametrize(
    "status", (InstanceStatus.CREATING, InstanceStatus.FAILED)
)
async def test_historical_personal_rows_do_not_restart_provisioning(
    session,
    status,
):
    user = await _user(session, status.value)
    session.add(
        Instance(
            cluster_id=f"historical-{status.value}",
            name="Historical personal instance",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=status,
            owner_user_id=user.id,
        )
    )
    await session.commit()
    background_tasks: set = set()

    result = await resolve_target_instance(
        user,
        session,
        session_factory=object(),
        background_tasks=background_tasks,
    )

    assert isinstance(result, dict)
    assert _error_code(result) == "NO_INSTANCE_ASSIGNED"
    assert background_tasks == set()
