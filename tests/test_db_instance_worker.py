from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig
from server.core.adapter_registry import AdapterRegistry
from server.core.db_instance_dispatcher import DBInstanceDispatcher
from server.core.db_instance_service import delete_db_instance_resource
from server.core.db_instance_worker import DBInstanceResourceWorker
from server.models import (
    Agent,
    Base,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceTopology,
    LeaseCleanupStep,
    LeaseProvisioningStep,
    ProvisioningBackend,
    ProvisioningCapacity,
    User,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 25, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
async def worker_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/worker.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        creator = User(external_id="admin", display_name="Admin")
        agent = Agent(name="worker-agent")
        instance = Instance(
            cluster_id="pc-worker",
            name="Worker",
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
            max_active_resources=10,
        )
        session.add(backend)
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="worker-1",
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
        resource_id = resource.id

    adapter = AsyncMock()

    async def advance_create(resource):
        resource.provisioning_step = {
            LeaseProvisioningStep.PENDING: LeaseProvisioningStep.RESOURCE_CONFIG_CREATED,
            LeaseProvisioningStep.RESOURCE_CONFIG_CREATED: LeaseProvisioningStep.TENANT_CREATED,
            LeaseProvisioningStep.TENANT_CREATED: LeaseProvisioningStep.USER_CREATED,
            LeaseProvisioningStep.USER_CREATED: LeaseProvisioningStep.DATABASE_CREATED,
            LeaseProvisioningStep.DATABASE_CREATED: LeaseProvisioningStep.GRANTED,
        }[resource.provisioning_step]

    async def verify(resource):
        resource.provisioning_step = LeaseProvisioningStep.VERIFIED

    adapter.create.side_effect = advance_create
    adapter.verify.side_effect = verify
    adapter.advance_create = advance_create
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        adapter,
    )
    clock = MutableClock()
    config = TenantProvisioningConfig(
        worker_poll_interval_seconds=1,
        worker_claim_ttl_seconds=10,
        worker_claim_renew_seconds=1,
        worker_max_retries=2,
        worker_initial_backoff_seconds=1,
        worker_max_backoff_seconds=4,
    )

    async def load():
        async with factory() as session:
            return await session.get(DBInstanceResource, resource_id)

    yield factory, resource_id, load, adapter, registry, clock, config
    await engine.dispose()


def _dispatcher(factory, config, registry, worker_id, clock):
    return DBInstanceDispatcher(
        factory,
        config,
        registry,
        worker_id=worker_id,
        clock=clock,
    )


async def test_worker_advances_resource_to_ready(worker_env):
    factory, _resource_id, load, adapter, registry, clock, config = worker_env
    worker = _dispatcher(factory, config, registry, "worker-a", clock)

    assert await worker.run_once() is True

    resource = await load()
    assert resource.status == DBInstanceStatus.READY
    assert adapter.create.await_count == 5
    adapter.verify.assert_awaited_once()
    assert resource.worker_id is None


async def test_two_workers_claim_resource_exclusively(worker_env):
    factory, _resource_id, load, adapter, registry, clock, config = worker_env
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_create(_resource):
        entered.set()
        await release.wait()
        await adapter.advance_create(_resource)

    adapter.create.side_effect = slow_create
    first = _dispatcher(factory, config, registry, "worker-a", clock)
    second = _dispatcher(factory, config, registry, "worker-b", clock)
    task = asyncio.create_task(first.run_once())
    await entered.wait()

    assert await second.run_once() is False
    release.set()
    assert await task is True
    assert (await load()).status == DBInstanceStatus.READY


async def test_file_sqlite_delete_waits_for_claim_commit(worker_env):
    factory, resource_id, load, _adapter, _registry, clock, config = worker_env
    resource = await load()
    agent_id = resource.owner_agent_id
    claim_entered = asyncio.Event()
    release_claim = asyncio.Event()
    worker = DBInstanceResourceWorker(
        factory, config, "worker-claim-race", clock
    )
    clock.value = datetime.now(timezone.utc)

    async def hold_claim(_session, _resource):
        claim_entered.set()
        await release_claim.wait()

    worker._before_claim_commit = hold_claim  # type: ignore[method-assign]
    claim_task = asyncio.create_task(worker.claim_one())
    await claim_entered.wait()

    async def delete():
        async with factory() as session:
            await session.execute(
                select(Agent.id).where(Agent.id == agent_id)
            )
            await session.rollback()
            return await delete_db_instance_resource(
                session, agent_id, resource_id
            )

    delete_task = asyncio.create_task(delete())
    await asyncio.sleep(0)
    assert not delete_task.done()
    release_claim.set()
    claimed_id, deleted = await asyncio.gather(claim_task, delete_task)

    stored = await load()
    assert claimed_id == resource_id
    assert deleted.id == resource_id
    assert stored.status == DBInstanceStatus.DELETING
    assert stored.worker_id == "worker-claim-race"


async def test_worker_retries_with_backoff_then_recovers(worker_env):
    factory, _resource_id, load, adapter, registry, clock, config = worker_env
    attempts = 0

    async def flaky_create(resource):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("secret")
        await adapter.advance_create(resource)

    adapter.create.side_effect = flaky_create
    worker = _dispatcher(factory, config, registry, "worker-a", clock)

    await worker.run_once()
    failed = await load()
    assert failed.status == DBInstanceStatus.CREATING
    assert failed.retry_count == 1
    assert failed.next_retry_at.replace(tzinfo=timezone.utc) == clock() + timedelta(
        seconds=1
    )
    assert "secret" not in failed.failure_reason
    assert await worker.run_once() is False

    clock.advance(1)
    assert await worker.run_once() is True
    assert (await load()).status == DBInstanceStatus.READY


async def test_expired_claim_is_recovered_after_process_restart(worker_env):
    factory, resource_id, load, adapter, registry, clock, config = worker_env
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.worker_id = "crashed"
        resource.worker_lease_until = clock() - timedelta(seconds=1)
        await session.commit()

    recovered = _dispatcher(factory, config, registry, "worker-b", clock)

    assert await recovered.run_once() is True
    assert (await load()).status == DBInstanceStatus.READY


async def test_exhausted_create_retries_fail_closed_and_queue_cleanup(worker_env):
    factory, _resource_id, load, adapter, registry, clock, config = worker_env
    adapter.create.side_effect = RuntimeError("backend unavailable")
    worker = _dispatcher(factory, config, registry, "worker-a", clock)

    for _ in range(3):
        await worker.run_once()
        clock.advance(10)

    resource = await load()
    assert resource.status == DBInstanceStatus.FAILED
    assert resource.cleanup_required is True
    assert resource.retry_count == 0
    assert resource.worker_id is None

    cleanup_attempts = 0

    async def flaky_cleanup(resource):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise RuntimeError("temporary cleanup failure")
        resource.cleanup_step = {
            LeaseCleanupStep.PENDING: LeaseCleanupStep.DATABASE_DROPPED,
            LeaseCleanupStep.DATABASE_DROPPED: LeaseCleanupStep.TENANT_DROPPED,
            LeaseCleanupStep.TENANT_DROPPED: LeaseCleanupStep.RESOURCE_CONFIG_DROPPED,
            LeaseCleanupStep.RESOURCE_CONFIG_DROPPED: LeaseCleanupStep.RESIDUE_VERIFIED,
        }[resource.cleanup_step]

    adapter.delete.side_effect = flaky_cleanup
    assert await worker.run_once() is True
    transient = await load()
    assert transient.status == DBInstanceStatus.FAILED
    assert transient.cleanup_required is True
    assert transient.retry_count == 1

    clock.advance(1)
    assert await worker.run_once() is True
    cleaned = await load()
    assert cleaned.status == DBInstanceStatus.FAILED
    assert cleaned.cleanup_required is False
    assert cleaned.capacity_released_at is not None
