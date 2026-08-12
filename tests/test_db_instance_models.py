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


async def _seed_permission_revision(session: AsyncSession):
    template = models.PermissionTemplate(name="default-dedicated")
    revision = models.PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    session.add_all([template, revision])
    await session.flush()
    return revision


def _dedicated_pool(
    name: str,
    revision: models.PermissionTemplateRevision,
) -> models.DedicatedPool:
    return models.DedicatedPool(
        name=name,
        target_size=1,
        max_total_members=3,
        max_member_purchases_per_hour=2,
        max_create_requests_per_agent_per_hour=10,
        max_delete_requests_per_agent_per_hour=10,
        purchase_config_json='{"db_node_class":"polar.mysql.x4.large"}',
        region_id="cn-hangzhou",
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )


def test_resource_step_enums_do_not_overload_external_states():
    assert "ready" not in {step.value for step in models.LeaseProvisioningStep}
    assert "deleted" not in {step.value for step in models.LeaseCleanupStep}


def test_dedicated_console_models_expose_typed_profile_and_routing() -> None:
    assert models.DedicatedPool.purchase_profile_id.property.columns[0].nullable
    assert (
        models.DedicatedPool.purchase_profile_revision.property.columns[0].nullable
    )
    assert models.DedicatedPool.storage_type.property.columns[0].nullable
    assert models.AgentProvisioningBinding.routing_order.property.columns[0].nullable
    assert models.DedicatedWorkerHeartbeat.__tablename__ == (
        "dedicated_worker_heartbeats"
    )


def test_dedicated_readiness_requires_two_complete_check_intervals():
    from server.models import dedicated_pool

    validate = getattr(
        dedicated_pool,
        "validate_readiness_schedule",
        None,
    )
    assert callable(validate)

    validate(check_interval_seconds=300, stale_after_seconds=600)
    with pytest.raises(
        ValueError,
        match="at least twice the health check interval",
    ):
        validate(check_interval_seconds=300, stale_after_seconds=599)


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


async def test_multiple_dedicated_backends_can_have_null_instance_ids(
    session: AsyncSession,
):
    revision = await _seed_permission_revision(session)
    pool_a = _dedicated_pool("pool-a", revision)
    pool_b = _dedicated_pool("pool-b", revision)
    backend_a = models.ProvisioningBackend(
        backend_type=models.ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=pool_a,
        max_active_resources=10,
    )
    backend_b = models.ProvisioningBackend(
        backend_type=models.ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=pool_b,
        max_active_resources=10,
    )
    session.add_all([backend_a, backend_b])

    await session.commit()

    assert backend_a.instance_id is None
    assert backend_b.instance_id is None
    assert backend_a.dedicated_pool_id != backend_b.dedicated_pool_id


async def test_backend_type_rejects_mismatched_instance_and_pool_targets(
    session: AsyncSession,
):
    revision = await _seed_permission_revision(session)
    pool = _dedicated_pool("pool-invalid-target", revision)
    instance = models.Instance(
        cluster_id="pc-invalid-target",
        name="invalid-target",
        topology=models.InstanceTopology.SINGLE_TENANT,
        allocation_mode=models.AllocationMode.REGISTERED,
    )
    session.add(
        models.ProvisioningBackend(
            backend_type=models.ProvisioningBackendType.DEDICATED_POOL,
            dedicated_pool=pool,
            instance=instance,
            max_active_resources=10,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()


async def test_physical_instance_can_belong_to_only_one_dedicated_member(
    session: AsyncSession,
):
    revision = await _seed_permission_revision(session)
    pool = _dedicated_pool("pool-member-unique", revision)
    instance = models.Instance(
        cluster_id="pc-member-unique",
        name="member-unique",
        topology=models.InstanceTopology.SINGLE_TENANT,
        allocation_mode=models.AllocationMode.POOLED,
    )
    session.add(
        models.DedicatedPoolMember(pool=pool, instance=instance)
    )
    await session.commit()

    session.add(
        models.DedicatedPoolMember(pool=pool, instance=instance)
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_resource_can_be_allocated_to_only_one_dedicated_member(
    session: AsyncSession,
):
    revision = await _seed_permission_revision(session)
    pool = _dedicated_pool("pool-resource-unique", revision)
    backend = models.ProvisioningBackend(
        backend_type=models.ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=pool,
        max_active_resources=10,
    )
    agent = models.Agent(name="dedicated-resource-owner")
    first_instance = models.Instance(
        cluster_id="pc-resource-unique-a",
        name="resource-unique-a",
        topology=models.InstanceTopology.SINGLE_TENANT,
        allocation_mode=models.AllocationMode.POOLED,
    )
    second_instance = models.Instance(
        cluster_id="pc-resource-unique-b",
        name="resource-unique-b",
        topology=models.InstanceTopology.SINGLE_TENANT,
        allocation_mode=models.AllocationMode.POOLED,
    )
    session.add_all(
        [backend, agent, first_instance, second_instance]
    )
    await session.flush()
    resource = models.DBInstanceResource(
        owner_agent_id=agent.id,
        backend_id=backend.id,
        client_token="dedicated-resource-unique",
        request_fingerprint="b" * 64,
        provisioning_mode=models.ProvisioningMode.DEDICATED,
        allocated_instance_id=first_instance.id,
    )
    session.add(resource)
    await session.flush()
    session.add_all(
        [
            models.DedicatedPoolMember(
                pool=pool,
                instance=first_instance,
                allocated_resource=resource,
            ),
            models.DedicatedPoolMember(
                pool=pool,
                instance=second_instance,
                allocated_resource=resource,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await session.commit()


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
