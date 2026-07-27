from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.core.backend_selector import tie_break_score
from server.core.provisioning_backend_repository import list_candidates
from server.models import (
    Agent,
    AgentProvisioningBinding,
    Base,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
    ProvisioningCapacity,
    User,
)
from server.models.base import utc_now


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


async def _add_backend(
    session: AsyncSession,
    *,
    agent: Agent,
    creator: User,
    suffix: str,
    priority: int,
    active_count: int,
    max_active_resources: int = 10,
    healthy: bool = True,
    status: ProvisioningBackendStatus = ProvisioningBackendStatus.ACTIVE,
    topology: InstanceTopology = InstanceTopology.MULTITENANT,
    checked_delta: timedelta = timedelta(),
) -> ProvisioningBackend:
    instance = Instance(
        cluster_id=f"cluster-{suffix}",
        name=f"Backend {suffix}",
        engine=InstanceEngine.POLARDB_MYSQL,
        topology=topology,
        status=InstanceStatus.ACTIVE,
        host=f"{suffix}.internal",
        port=3306,
    )
    session.add(instance)
    await session.flush()
    credential = InstanceCredential(
        instance_id=instance.id,
        name="provisioning-admin",
        purpose=CredentialPurpose.PROVISIONING_ADMIN,
        capability=CredentialCapability.ADMIN,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
        created_by_user_id=creator.id,
    )
    session.add(credential)
    await session.flush()
    backend = ProvisioningBackend(
        instance_id=instance.id,
        admin_credential_id=credential.id,
        status=status,
        priority=priority,
        max_active_resources=max_active_resources,
    )
    session.add(backend)
    await session.flush()
    session.add_all(
        [
            ProvisioningBackendHealth(
                backend_id=backend.id,
                healthy=healthy,
                checked_at=utc_now() - checked_delta,
            ),
            ProvisioningCapacity(
                scope_type="backend",
                scope_id=backend.id,
                active_count=active_count,
            ),
            AgentProvisioningBinding(
                agent_id=agent.id,
                backend_id=backend.id,
                enabled=True,
                created_by_user_id=creator.id,
            ),
        ]
    )
    await session.flush()
    return backend


async def test_selector_orders_priority_then_load_then_stable_hash(session):
    creator = User(external_id="admin", display_name="Admin")
    agent = Agent(name="production-agent", created_by=None)
    session.add_all([creator, agent])
    await session.flush()

    high_priority_high_load = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="high-load",
        priority=10,
        active_count=8,
    )
    low_priority = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="low-priority",
        priority=5,
        active_count=0,
    )
    high_priority_low_load = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="low-load",
        priority=10,
        active_count=2,
    )
    await session.commit()

    candidates = await list_candidates(
        session,
        agent.id,
        InstanceEngine.POLARDB_MYSQL,
        "deploy-42",
    )

    assert [item.backend_id for item in candidates] == [
        high_priority_low_load.id,
        high_priority_high_load.id,
        low_priority.id,
    ]


async def test_selector_uses_stable_hash_for_equal_priority_and_load(session):
    creator = User(external_id="admin", display_name="Admin")
    agent = Agent(name="production-agent")
    session.add_all([creator, agent])
    await session.flush()
    first = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="first",
        priority=10,
        active_count=1,
    )
    second = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="second",
        priority=10,
        active_count=1,
    )
    await session.commit()

    candidates = await list_candidates(
        session,
        agent.id,
        InstanceEngine.POLARDB_MYSQL,
        "deploy-42",
    )

    assert [candidate.backend_id for candidate in candidates] == sorted(
        [first.id, second.id],
        key=lambda backend_id: tie_break_score(agent.id, "deploy-42", backend_id),
    )


async def test_selector_excludes_ineligible_backends(session):
    creator = User(external_id="admin", display_name="Admin")
    agent = Agent(name="production-agent")
    session.add_all([creator, agent])
    await session.flush()
    eligible = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="eligible",
        priority=0,
        active_count=0,
    )
    await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="draining",
        priority=100,
        active_count=0,
        status=ProvisioningBackendStatus.DRAINING,
    )
    await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="unhealthy",
        priority=100,
        active_count=0,
        healthy=False,
    )
    await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="stale",
        priority=100,
        active_count=0,
        checked_delta=timedelta(minutes=5),
    )
    await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="disabled",
        priority=100,
        active_count=0,
        status=ProvisioningBackendStatus.DISABLED,
    )
    await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix="single",
        priority=100,
        active_count=0,
        topology=InstanceTopology.SINGLE_TENANT,
    )
    await session.commit()

    candidates = await list_candidates(
        session,
        agent.id,
        InstanceEngine.POLARDB_MYSQL,
        "deploy-42",
    )

    assert [candidate.backend_id for candidate in candidates] == [eligible.id]


@pytest.mark.parametrize(
    "status",
    [InstanceStatus.STOPPED, InstanceStatus.FAILED],
)
async def test_selector_excludes_inactive_physical_instance(session, status):
    creator = User(external_id="admin", display_name="Admin")
    agent = Agent(name="production-agent")
    session.add_all([creator, agent])
    await session.flush()
    backend = await _add_backend(
        session,
        agent=agent,
        creator=creator,
        suffix=status.value,
        priority=10,
        active_count=0,
    )
    instance = await session.get(Instance, backend.instance_id)
    instance.status = status
    await session.commit()

    candidates = await list_candidates(
        session,
        agent.id,
        InstanceEngine.POLARDB_MYSQL,
        "deploy-42",
    )

    assert candidates == []
