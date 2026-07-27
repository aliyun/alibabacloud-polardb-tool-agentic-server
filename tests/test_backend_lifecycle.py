from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.config import reset_config
from server.core.db_instance_service import (
    create_db_instance_resource,
    delete_db_instance_resource,
)
from server.models import Base, DBInstanceResource, DBInstanceStatus
from tests.test_db_instance_service import _seed_backend


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


@pytest.fixture(autouse=True)
def encryption_config(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_config()
    yield
    reset_config()


@pytest.mark.parametrize(
    "status",
    [
        DBInstanceStatus.CREATING,
        DBInstanceStatus.READY,
        DBInstanceStatus.FAILED,
        DBInstanceStatus.DELETE_FAILED,
    ],
)
async def test_delete_accepts_all_deletable_states(session, status):
    agent, _backend = await _seed_backend(session)
    resource = await create_db_instance_resource(
        session,
        agent_id=agent.id,
        client_token=f"delete-{status.value}",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = status
    await session.commit()

    result = await delete_db_instance_resource(
        session,
        resource.owner_agent_id,
        resource.id,
    )

    assert result.status == DBInstanceStatus.DELETING
    assert result.cleanup_required is True


@pytest.mark.parametrize(
    "status",
    [DBInstanceStatus.DELETING, DBInstanceStatus.DELETED],
)
async def test_delete_is_idempotent_for_terminal_transition_states(
    session,
    status,
):
    agent, _backend = await _seed_backend(session)
    resource = await create_db_instance_resource(
        session,
        agent_id=agent.id,
        client_token=f"idempotent-delete-{status.value}",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = status
    resource.cleanup_required = status == DBInstanceStatus.DELETING
    await session.commit()

    first = await delete_db_instance_resource(
        session,
        resource.owner_agent_id,
        resource.id,
    )
    second = await delete_db_instance_resource(
        session,
        resource.owner_agent_id,
        resource.id,
    )

    assert first.status == status
    assert second.status == status


async def test_deleted_resource_row_and_client_token_are_permanent(session):
    agent, _backend = await _seed_backend(session)
    agent_id = agent.id
    original = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="permanent-token",
        name="Orders",
        db_type="polardb_mysql",
    )
    original.status = DBInstanceStatus.DELETED
    original.cleanup_required = False
    original_id = original.id
    await session.commit()

    replay = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="permanent-token",
        name="Orders",
        db_type="polardb_mysql",
    )
    count = await session.scalar(
        select(func.count())
        .select_from(DBInstanceResource)
        .where(
            DBInstanceResource.owner_agent_id == agent_id,
            DBInstanceResource.client_token == "permanent-token",
        )
    )

    assert replay.id == original_id
    assert replay.status == DBInstanceStatus.DELETED
    assert count == 1
