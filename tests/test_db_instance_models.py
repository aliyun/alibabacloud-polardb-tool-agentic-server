from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import server.models as models


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session

    await engine.dispose()


async def _seed_agent_backend(session: AsyncSession):
    admin = models.User(external_id="admin-1", display_name="Admin 1")
    agent = models.Agent(name="Agent 1", creator=admin)
    instance = models.Instance(
        cluster_id="pc-multitenant-1",
        name="Multitenant",
        engine=models.InstanceEngine.POLARDB_MYSQL,
        topology=models.InstanceTopology.MULTITENANT,
        allocation_mode=models.AllocationMode.REGISTERED,
    )
    credential = models.InstanceCredential(
        instance=instance,
        name="provisioning-admin",
        purpose=models.CredentialPurpose.PROVISIONING_ADMIN,
        capability=models.CredentialCapability.ADMIN,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
        created_by=admin,
    )
    backend = models.ProvisioningBackend(
        instance=instance,
        admin_credential=credential,
        max_active_resources=10,
    )
    session.add_all([admin, agent, instance, credential, backend])
    await session.commit()
    return agent, backend


def test_resource_step_enums_do_not_overload_external_states():
    assert "ready" not in {step.value for step in models.LeaseProvisioningStep}
    assert "deleted" not in {step.value for step in models.LeaseCleanupStep}


async def test_resource_defaults_and_identifier_format(session: AsyncSession):
    agent, backend = await _seed_agent_backend(session)
    resource = models.DBInstanceResource(
        owner_agent_id=agent.id,
        backend_id=backend.id,
        client_token="client-token-1",
        request_fingerprint="a" * 64,
        tenant_name="t123456789",
    )
    session.add(resource)
    await session.commit()

    assert resource.id.startswith("dbi-")
    assert len(resource.id) == 36
    assert resource.engine == models.InstanceEngine.POLARDB_MYSQL
    assert resource.status == models.DBInstanceStatus.CREATING
    assert resource.fingerprint_version == 1
    assert resource.client_token == "client-token-1"
    assert resource.provisioning_step == models.LeaseProvisioningStep.PENDING
    assert resource.cleanup_step == models.LeaseCleanupStep.PENDING
    assert resource.cleanup_required is False
    assert resource.capacity_released_at is None


async def test_same_agent_client_token_is_unique(session: AsyncSession):
    agent, backend = await _seed_agent_backend(session)
    session.add(
        models.DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="client-token-1",
            request_fingerprint="a" * 64,
            tenant_name="t123456789",
        )
    )
    await session.commit()

    session.add(
        models.DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="client-token-1",
            request_fingerprint="a" * 64,
            tenant_name="t987654321",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_capacity_scope_is_unique(session: AsyncSession):
    session.add(models.ProvisioningCapacity(scope_type="agent", scope_id="agent-1"))
    await session.commit()
    session.add(models.ProvisioningCapacity(scope_type="agent", scope_id="agent-1"))

    with pytest.raises(IntegrityError):
        await session.commit()
