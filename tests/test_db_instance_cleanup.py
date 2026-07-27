from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig
from server.core.adapter_registry import AdapterRegistry
from server.core.db_instance_dispatcher import DBInstanceDispatcher
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
    ProvisioningBackend,
    ProvisioningBackendStatus,
    ProvisioningCapacity,
    User,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def cleanup_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cleanup.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        creator = User(external_id="admin", display_name="Admin")
        agent = Agent(name="cleanup-agent")
        instance = Instance(
            cluster_id="pc-cleanup",
            name="Cleanup",
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
            status=ProvisioningBackendStatus.DISABLED,
            max_active_resources=10,
        )
        session.add(backend)
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="cleanup-1",
            request_fingerprint="a" * 64,
            tenant_name="t123456789",
            resource_config_name="rc_t123456789",
            database_name="agentic@t123456789",
            status=DBInstanceStatus.DELETING,
            cleanup_required=True,
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
        agent_id = agent.id
        backend_id = backend.id
    adapter = AsyncMock()

    async def finish_cleanup(resource):
        resource.cleanup_step = {
            LeaseCleanupStep.PENDING: LeaseCleanupStep.DATABASE_DROPPED,
            LeaseCleanupStep.DATABASE_DROPPED: LeaseCleanupStep.TENANT_DROPPED,
            LeaseCleanupStep.TENANT_DROPPED: LeaseCleanupStep.RESOURCE_CONFIG_DROPPED,
            LeaseCleanupStep.RESOURCE_CONFIG_DROPPED: LeaseCleanupStep.RESIDUE_VERIFIED,
        }[resource.cleanup_step]

    adapter.delete.side_effect = finish_cleanup
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        adapter,
    )
    clock = Clock()
    config = TenantProvisioningConfig(
        worker_claim_ttl_seconds=10,
        worker_claim_renew_seconds=1,
        worker_max_retries=1,
        worker_initial_backoff_seconds=1,
        worker_max_backoff_seconds=1,
    )
    dispatcher = DBInstanceDispatcher(
        factory,
        config,
        registry,
        worker_id="cleanup-worker",
        clock=clock,
    )

    async def load():
        async with factory() as session:
            return await session.get(DBInstanceResource, resource_id)

    yield (
        factory,
        dispatcher,
        adapter,
        resource_id,
        agent_id,
        backend_id,
        load,
        clock,
    )
    await engine.dispose()


async def test_cleanup_runs_for_disabled_backend_and_marks_deleted(cleanup_env):
    (
        factory,
        dispatcher,
        adapter,
        resource_id,
        agent_id,
        backend_id,
        load,
        _clock,
    ) = cleanup_env

    assert await dispatcher.run_once() is True

    resource = await load()
    assert resource.status == DBInstanceStatus.DELETED
    assert resource.cleanup_required is False
    assert resource.capacity_released_at is not None
    assert adapter.delete.await_count == 4
    assert adapter.delete.await_args.args[0].id == resource_id
    async with factory() as session:
        credential = (
            (
                await session.execute(
                    select(InstanceCredential).where(
                        InstanceCredential.resource_id == resource_id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert credential.status == CredentialStatus.REVOKED
        assert credential.username_ciphertext is None
        assert credential.password_ciphertext is None
        counts = {
            (row.scope_type, row.scope_id): row.active_count
            for row in (
                await session.execute(select(ProvisioningCapacity))
            ).scalars()
        }
        assert counts[("agent", agent_id)] == 0
        assert counts[("backend", backend_id)] == 0


async def test_failed_resource_cleanup_preserves_failed_history(cleanup_env):
    factory, dispatcher, adapter, resource_id, *_rest = cleanup_env
    load = _rest[-2]
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.status = DBInstanceStatus.FAILED
        await session.commit()

    await dispatcher.run_once()

    resource = await load()
    assert resource.status == DBInstanceStatus.FAILED
    assert resource.cleanup_required is False
    assert adapter.delete.await_count == 4


async def test_cleanup_retries_then_enters_delete_failed(cleanup_env):
    (
        _factory,
        dispatcher,
        adapter,
        _resource_id,
        _agent_id,
        _backend_id,
        load,
        clock,
    ) = cleanup_env
    adapter.delete.side_effect = RuntimeError("secret backend failure")

    await dispatcher.run_once()
    first = await load()
    assert first.status == DBInstanceStatus.DELETING
    assert first.next_retry_at is not None
    assert "secret" not in first.failure_reason

    clock.advance(1)
    await dispatcher.run_once()
    failed = await load()
    assert failed.status == DBInstanceStatus.DELETE_FAILED
    assert failed.cleanup_required is True
    assert failed.worker_id is None


async def test_residue_verification_failure_keeps_capacity_and_credentials(
    cleanup_env,
):
    (
        factory,
        dispatcher,
        adapter,
        resource_id,
        agent_id,
        backend_id,
        load,
        clock,
    ) = cleanup_env

    async def fail_residue_verification(resource):
        if (
            resource.cleanup_step
            == LeaseCleanupStep.RESOURCE_CONFIG_DROPPED
        ):
            raise RuntimeError("residue remains")
        resource.cleanup_step = {
            LeaseCleanupStep.PENDING:
                LeaseCleanupStep.DATABASE_DROPPED,
            LeaseCleanupStep.DATABASE_DROPPED:
                LeaseCleanupStep.TENANT_DROPPED,
            LeaseCleanupStep.TENANT_DROPPED:
                LeaseCleanupStep.RESOURCE_CONFIG_DROPPED,
        }[resource.cleanup_step]

    adapter.delete.side_effect = fail_residue_verification

    assert await dispatcher.run_once() is True
    pending = await load()
    assert pending.status == DBInstanceStatus.DELETING
    assert pending.cleanup_step == LeaseCleanupStep.RESOURCE_CONFIG_DROPPED
    assert pending.capacity_released_at is None

    async with factory() as session:
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource_id
                )
            )
        ).scalar_one()
        capacities = {
            (row.scope_type, row.scope_id): row.active_count
            for row in (
                await session.execute(select(ProvisioningCapacity))
            ).scalars()
        }
        assert credential.status == CredentialStatus.ACTIVE
        assert credential.username_ciphertext == "resource-user"
        assert credential.password_ciphertext == "resource-password"
        assert capacities[("agent", agent_id)] == 1
        assert capacities[("backend", backend_id)] == 1

    clock.advance(1)
    assert await dispatcher.run_once() is True
    failed = await load()
    assert failed.status == DBInstanceStatus.DELETE_FAILED
    assert failed.capacity_released_at is None


@pytest.mark.parametrize(
    "corruption",
    ["agent_zero", "backend_zero", "agent_missing", "backend_missing"],
)
async def test_capacity_invariant_failure_rolls_back_terminal_cleanup(
    cleanup_env,
    corruption,
):
    (
        factory,
        dispatcher,
        _adapter,
        resource_id,
        agent_id,
        backend_id,
        load,
        clock,
    ) = cleanup_env
    scope_type, scope_id = (
        ("agent", agent_id)
        if corruption.startswith("agent")
        else ("backend", backend_id)
    )
    async with factory() as session:
        capacity = (
            await session.execute(
                select(ProvisioningCapacity).where(
                    ProvisioningCapacity.scope_type == scope_type,
                    ProvisioningCapacity.scope_id == scope_id,
                )
            )
        ).scalar_one()
        if corruption.endswith("zero"):
            capacity.active_count = 0
        else:
            await session.delete(capacity)
        await session.commit()

    assert await dispatcher.run_once() is True

    resource = await load()
    assert resource.status == DBInstanceStatus.DELETING
    assert resource.cleanup_required is True
    assert resource.capacity_released_at is None
    async with factory() as session:
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource_id
                )
            )
        ).scalar_one()
        assert credential.status == CredentialStatus.ACTIVE
        assert credential.username_ciphertext == "resource-user"
        assert credential.password_ciphertext == "resource-password"
        capacity = (
            await session.execute(
                select(ProvisioningCapacity).where(
                    ProvisioningCapacity.scope_type == scope_type,
                    ProvisioningCapacity.scope_id == scope_id,
                )
            )
        ).scalar_one_or_none()
        if capacity is None:
            session.add(
                ProvisioningCapacity(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    active_count=1,
                )
            )
        else:
            capacity.active_count = 1
        await session.commit()

    clock.advance(1)
    assert await dispatcher.run_once() is True
    recovered = await load()
    assert recovered.status == DBInstanceStatus.DELETED
    assert recovered.capacity_released_at is not None
