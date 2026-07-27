from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig
from server.core.adapter_registry import AdapterNotFound, AdapterRegistry
from server.core.db_instance_dispatcher import DBInstanceDispatcher
from server.core.db_instance_service import delete_db_instance_resource
from server.models import (
    Agent,
    Base,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceTopology,
    LeaseCleanupStep,
    LeaseProvisioningStep,
    ProvisioningBackend,
    ProvisioningBackendStatus,
    ProvisioningCapacity,
    User,
)


class StepAdapter:
    def __init__(self) -> None:
        self.create_entered = asyncio.Event()
        self.create_release = asyncio.Event()
        self.block_create = False
        self.create_error: Exception | None = None
        self.delete_calls: list[str] = []
        self.delete_entered = asyncio.Event()
        self.delete_release = asyncio.Event()
        self.block_first_delete = False

    async def create(self, resource):
        self.create_entered.set()
        if self.block_create:
            await self.create_release.wait()
        if self.create_error is not None:
            raise self.create_error
        resource.provisioning_step = {
            LeaseProvisioningStep.PENDING: LeaseProvisioningStep.RESOURCE_CONFIG_CREATED,
            LeaseProvisioningStep.RESOURCE_CONFIG_CREATED: LeaseProvisioningStep.TENANT_CREATED,
            LeaseProvisioningStep.TENANT_CREATED: LeaseProvisioningStep.USER_CREATED,
            LeaseProvisioningStep.USER_CREATED: LeaseProvisioningStep.DATABASE_CREATED,
            LeaseProvisioningStep.DATABASE_CREATED: LeaseProvisioningStep.GRANTED,
        }[resource.provisioning_step]

    async def verify(self, resource):
        resource.provisioning_step = LeaseProvisioningStep.VERIFIED

    async def delete(self, resource):
        self.delete_calls.append(resource.id)
        self.delete_entered.set()
        if self.block_first_delete and len(self.delete_calls) == 1:
            await self.delete_release.wait()
        resource.cleanup_step = {
            LeaseCleanupStep.PENDING: LeaseCleanupStep.DATABASE_DROPPED,
            LeaseCleanupStep.DATABASE_DROPPED: LeaseCleanupStep.TENANT_DROPPED,
            LeaseCleanupStep.TENANT_DROPPED: LeaseCleanupStep.RESOURCE_CONFIG_DROPPED,
            LeaseCleanupStep.RESOURCE_CONFIG_DROPPED: LeaseCleanupStep.RESIDUE_VERIFIED,
            LeaseCleanupStep.RESIDUE_VERIFIED: LeaseCleanupStep.RESIDUE_VERIFIED,
        }[resource.cleanup_step]

    async def health_check(self, backend):
        del backend


@pytest.fixture
async def race_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/races.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        creator = User(external_id="admin", display_name="Admin")
        agent = Agent(name="race-agent")
        instance = Instance(
            cluster_id="pc-race",
            name="Race",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
        )
        session.add_all([creator, agent, instance])
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext="u",
            password_ciphertext="p",
            created_by_user_id=creator.id,
        )
        session.add(credential)
        await session.flush()
        backend = ProvisioningBackend(
            instance_id=instance.id,
            admin_credential_id=credential.id,
            status=ProvisioningBackendStatus.ACTIVE,
            max_active_resources=10,
        )
        session.add(backend)
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="race-1",
            request_fingerprint="a" * 64,
            tenant_name="t123456789",
            resource_config_name="rc_t123456789",
            database_name="agentic@t123456789",
        )
        session.add(resource)
        await session.flush()
        session.add_all(
            [
                InstanceCredential(
                    resource_id=resource.id,
                    name="resource-access",
                    purpose=CredentialPurpose.RESOURCE_ACCESS,
                    capability=CredentialCapability.READWRITE,
                    username_ciphertext="resource-user",
                    password_ciphertext="resource-password",
                    database_name=resource.database_name,
                ),
                ProvisioningCapacity(
                    scope_type="agent",
                    scope_id=agent.id,
                    active_count=1,
                ),
                ProvisioningCapacity(
                    scope_type="backend",
                    scope_id=backend.id,
                    active_count=1,
                ),
            ]
        )
        await session.commit()
        resource_id, backend_id, agent_id = resource.id, backend.id, agent.id

    adapter = StepAdapter()
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        adapter,
    )
    def clock():
        return datetime.now(timezone.utc)

    def dispatcher(worker_id, *, max_retries=5):
        return DBInstanceDispatcher(
            factory,
            TenantProvisioningConfig(
                worker_claim_ttl_seconds=10,
                worker_claim_renew_seconds=9,
                worker_max_retries=max_retries,
            ),
            registry,
            worker_id=worker_id,
            clock=clock,
        )

    yield factory, dispatcher, adapter, resource_id, backend_id, agent_id
    await engine.dispose()


async def test_concurrent_delete_wins_over_inflight_forward_step(race_env):
    factory, dispatcher, adapter, resource_id, _backend_id, agent_id = race_env
    adapter.block_create = True
    task = asyncio.create_task(dispatcher("worker-a").run_once())
    await adapter.create_entered.wait()
    async with factory() as session:
        await delete_db_instance_resource(session, agent_id, resource_id)

    assert await dispatcher("worker-b").run_once() is False
    async with factory() as session:
        pending = await session.get(DBInstanceResource, resource_id)
        assert pending.status == DBInstanceStatus.DELETING
        assert pending.worker_id == "worker-a"
        assert pending.capacity_released_at is None

    adapter.create_release.set()
    await task
    assert await dispatcher("worker-b").run_once() is True

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource.status == DBInstanceStatus.DELETED
        assert resource.provisioning_step == LeaseProvisioningStep.PENDING
        assert resource.worker_id is None
        assert resource.capacity_released_at is not None
    assert adapter.delete_calls == [resource_id] * 4


async def test_forward_failure_after_delete_does_not_consume_cleanup_retry(
    race_env,
):
    factory, dispatcher, adapter, resource_id, _backend_id, agent_id = race_env
    adapter.block_create = True
    adapter.create_error = RuntimeError("forward failed")
    task = asyncio.create_task(
        dispatcher("worker-a", max_retries=0).run_once()
    )
    await adapter.create_entered.wait()
    async with factory() as session:
        await delete_db_instance_resource(session, agent_id, resource_id)

    adapter.create_release.set()
    await task

    async with factory() as session:
        pending = await session.get(DBInstanceResource, resource_id)
        assert pending.status == DBInstanceStatus.DELETING
        assert pending.cleanup_required is True
        assert pending.retry_count == 0
        assert pending.worker_id is None

    assert (
        await dispatcher("worker-b", max_retries=0).run_once()
        is True
    )
    async with factory() as session:
        deleted = await session.get(DBInstanceResource, resource_id)
        assert deleted.status == DBInstanceStatus.DELETED
        assert deleted.capacity_released_at is not None
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource_id
                )
            )
        ).scalar_one()
        assert credential.status == CredentialStatus.REVOKED
        assert credential.username_ciphertext is None
        assert credential.password_ciphertext is None


async def test_disable_before_forward_persist_queues_cleanup(race_env):
    factory, dispatcher, adapter, resource_id, backend_id, _agent_id = race_env
    adapter.block_create = True
    task = asyncio.create_task(dispatcher("worker-a").run_once())
    await adapter.create_entered.wait()
    async with factory() as session:
        backend = await session.get(ProvisioningBackend, backend_id)
        backend.status = ProvisioningBackendStatus.DISABLED
        await session.commit()

    adapter.create_release.set()
    await task

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource.status == DBInstanceStatus.FAILED
        assert resource.cleanup_required is True
        assert resource.provisioning_step == LeaseProvisioningStep.PENDING


async def test_lost_claim_never_commits_stale_forward_result(race_env):
    factory, dispatcher, adapter, resource_id, _backend_id, _agent_id = race_env
    adapter.block_create = True
    task = asyncio.create_task(dispatcher("worker-a").run_once())
    await adapter.create_entered.wait()
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.worker_id = "worker-b"
        resource.worker_lease_until = datetime.now(timezone.utc) + timedelta(
            minutes=1
        )
        await session.commit()

    adapter.create_release.set()
    await task

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource.status == DBInstanceStatus.CREATING
        assert resource.provisioning_step == LeaseProvisioningStep.PENDING
        assert resource.worker_id == "worker-b"


async def test_two_dispatchers_finish_cleanup_exactly_once(race_env):
    factory, dispatcher, adapter, resource_id, _backend_id, _agent_id = race_env
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.status = DBInstanceStatus.DELETING
        resource.cleanup_required = True
        resource.cleanup_step = LeaseCleanupStep.RESOURCE_CONFIG_DROPPED
        await session.commit()

    adapter.block_first_delete = True
    first = asyncio.create_task(dispatcher("worker-a").run_once())
    await adapter.delete_entered.wait()
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.worker_lease_until = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )
        await session.commit()

    assert await dispatcher("worker-b").run_once() is True
    adapter.delete_release.set()
    await first

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource.status == DBInstanceStatus.DELETED
        assert resource.capacity_released_at is not None
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource_id
                )
            )
        ).scalar_one()
        assert credential.status == CredentialStatus.REVOKED
        capacities = (
            await session.execute(select(ProvisioningCapacity))
        ).scalars()
        assert [row.active_count for row in capacities] == [0, 0]
    assert adapter.delete_calls == [resource_id, resource_id]


async def test_missing_adapter_failure_isolated_from_next_resource(race_env):
    factory, dispatcher, adapter, resource_id, _backend_id, _agent_id = race_env
    async with factory() as session:
        first = await session.get(DBInstanceResource, resource_id)
        second = DBInstanceResource(
            owner_agent_id=first.owner_agent_id,
            backend_id=first.backend_id,
            client_token="race-2",
            request_fingerprint="b" * 64,
            tenant_name="t987654321",
            resource_config_name="rc_t987654321",
            database_name="agentic@t987654321",
            created_at=first.created_at + timedelta(seconds=1),
        )
        session.add(second)
        await session.commit()
        second_id = second.id

    worker = dispatcher("worker-a")
    real_get = worker._registry.get
    calls = 0

    def flaky_get(engine, topology):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AdapterNotFound("unsupported")
        return real_get(engine, topology)

    worker._registry.get = flaky_get

    assert await worker.run_once() is True
    assert await worker.run_once() is True

    async with factory() as session:
        failed = await session.get(DBInstanceResource, resource_id)
        progressed = await session.get(DBInstanceResource, second_id)
        assert failed.status == DBInstanceStatus.CREATING
        assert failed.retry_count == 1
        assert failed.failure_reason == (
            "Provisioning step failed with AdapterNotFound"
        )
        assert progressed.status == DBInstanceStatus.READY


async def test_cleanup_finalization_resumes_from_residue_verified(race_env):
    """A resource stuck at RESIDUE_VERIFIED must finalize instead of
    deadlocking on 'invalid cleanup step'."""
    factory, dispatcher, adapter, resource_id, backend_id, agent_id = race_env
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.status = DBInstanceStatus.DELETING
        resource.cleanup_required = True
        resource.cleanup_step = LeaseCleanupStep.RESIDUE_VERIFIED
        await session.commit()

    worker = dispatcher("worker-finalize")
    assert await worker.run_once() is True

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource.status == DBInstanceStatus.DELETED
        assert resource.capacity_released_at is not None
        assert resource.worker_id is None
        capacities = (
            await session.execute(select(ProvisioningCapacity))
        ).scalars().all()
        assert all(c.active_count == 0 for c in capacities)
        credentials = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource_id
                )
            )
        ).scalars().all()
        assert all(c.status == CredentialStatus.REVOKED for c in credentials)
