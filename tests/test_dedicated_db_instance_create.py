from __future__ import annotations

import base64
import json
import os
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from unittest.mock import AsyncMock

from server.config import reset_config
from server.config import TenantProvisioningConfig
from server.core.crypto import encrypt
from server.core.db_instance_application_service import (
    CreateDBInstanceCommand,
    DBInstanceApplicationService,
)
from server.core.db_instance_worker import DBInstanceResourceWorker
from server.core.dedicated_pool_worker import DedicatedPoolWorker
from server.core.dedicated_purchase_profile import (
    PROFILE_ID,
    PROFILE_REVISION,
    build_purchase_config,
)
from server.models import (
    Agent,
    AgentProvisioningBinding,
    AllocationMode,
    Base,
    DBInstanceResource,
    DBInstanceStatus,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    Instance,
    InstanceStatus,
    InstanceTopology,
    PermissionTemplate,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendType,
    ProvisioningMode,
    ReadinessStatus,
    User,
)
from server.models.base import utc_now


@pytest.fixture(autouse=True)
def encryption_config(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_config()
    yield
    reset_config()


@pytest.fixture
async def dedicated_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        creator = User(external_id="dedicated-admin", display_name="Admin")
        agent = Agent(name="dedicated-agent", max_active_resources=10)
        template = PermissionTemplate(name="dedicated-template")
        revision = PermissionTemplateRevision(
            template=template,
            revision=1,
            privileges_json='["SELECT","INSERT"]',
        )
        pool = DedicatedPool(
            name="dedicated-pool",
            target_size=1,
            max_total_members=3,
            max_member_purchases_per_hour=3,
            max_create_requests_per_agent_per_hour=10,
            max_delete_requests_per_agent_per_hour=10,
            purchase_config_json=json.dumps(
                build_purchase_config(storage_type="essdpl1"),
                separators=(",", ":"),
                sort_keys=True,
            ),
            purchase_profile_id=PROFILE_ID,
            purchase_profile_revision=PROFILE_REVISION,
            storage_type="essdpl1",
            region_id="cn-hangzhou",
            vpc_id="vpc-dedicated",
            vswitch_id="vsw-dedicated",
            permission_template_revision=revision,
        )
        backend = ProvisioningBackend(
            backend_type=ProvisioningBackendType.DEDICATED_POOL,
            dedicated_pool=pool,
            permission_template_revision=revision,
            max_active_resources=10,
        )
        instance = Instance(
            cluster_id="pc-hot-member",
            name="Hot member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.DEDICATED_POOL,
            status=InstanceStatus.ACTIVE,
            region="cn-hangzhou",
            host="hot.internal",
            port=3306,
        )
        session.add_all([creator, agent, backend, instance])
        await session.flush()
        member = DedicatedPoolMember(
            pool=pool,
            instance=instance,
            status=DedicatedMemberStatus.AVAILABLE,
            readiness_status=ReadinessStatus.FRESH,
            last_ready_verified_at=utc_now(),
            host="hot.internal",
            port=3306,
            database_name="agentic",
            sandbox_username_ciphertext=encrypt("agentic"),
            sandbox_password_ciphertext=encrypt("sandbox-secret"),
            permission_template_revision=revision,
            permission_snapshot_json=(
                '{"grant_option":false,"legacy":false,'
                '"privileges":["SELECT","INSERT"],'
                '"revision_id":"' + revision.id + '",'
                '"scope":"global","template_id":"' + template.id + '"}'
            ),
        )
        session.add_all(
            [
                member,
                ProvisioningBackendHealth(
                    backend=backend,
                    healthy=True,
                    checked_at=utc_now(),
                ),
                AgentProvisioningBinding(
                    agent=agent,
                    backend=backend,
                    routing_order=0,
                    created_by=creator,
                ),
            ]
        )
        await session.commit()
        yield session, agent.id, pool.id, member.id
    await engine.dispose()


def _command(token: str) -> CreateDBInstanceCommand:
    return CreateDBInstanceCommand(
        agent_id="filled-by-test",
        mode=ProvisioningMode.DEDICATED,
        idempotency_key=token,
        name="Orders",
        db_type="polardb_mysql",
    )


async def test_hot_create_atomically_returns_ready_connection(dedicated_context):
    session, agent_id, _pool_id, member_id = dedicated_context
    command = _command("dedicated-hot")
    command = CreateDBInstanceCommand(
        agent_id=agent_id,
        mode=command.mode,
        idempotency_key=command.idempotency_key,
        name=command.name,
        db_type=command.db_type,
    )

    created = await DBInstanceApplicationService(session).create(command)

    assert created.status == "READY"
    assert created.connection is not None
    assert created.connection.host == "hot.internal"
    assert created.connection.port == 3306
    assert created.connection.database == "agentic"
    assert created.connection.username == "agentic"
    assert created.connection.password == "sandbox-secret"
    member = await session.get(DedicatedPoolMember, member_id)
    assert member.status == DedicatedMemberStatus.ALLOCATED
    assert member.allocated_resource_id == created.resource_id


async def test_stale_member_is_skipped_while_request_reserves_cold_member(
    dedicated_context,
):
    session, agent_id, pool_id, member_id = dedicated_context
    member = await session.get(DedicatedPoolMember, member_id)
    member.readiness_status = ReadinessStatus.STALE
    member.last_ready_verified_at = utc_now() - timedelta(hours=1)
    await session.commit()

    command = _command("dedicated-stale")
    created = await DBInstanceApplicationService(session).create(
        CreateDBInstanceCommand(
            agent_id=agent_id,
            mode=command.mode,
            idempotency_key=command.idempotency_key,
            name=command.name,
            db_type=command.db_type,
        )
    )

    assert created.status == "CREATING"
    assert created.connection is None
    member = await session.get(DedicatedPoolMember, member_id)
    assert member.status == DedicatedMemberStatus.AVAILABLE
    assert member.allocated_resource_id is None
    members = list(
        (
            await session.execute(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.pool_id == pool_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(members) == 2
    cold_member = next(value for value in members if value.id != member_id)
    assert cold_member.status == DedicatedMemberStatus.ALLOCATED_PREPARING
    assert cold_member.allocated_resource_id == created.resource_id


async def test_stale_member_recheck_does_not_steal_cold_reserved_request(
    dedicated_context,
):
    session, agent_id, _pool_id, member_id = dedicated_context
    member = await session.get(DedicatedPoolMember, member_id)
    member.readiness_status = ReadinessStatus.STALE
    await session.commit()
    command = _command("dedicated-recheck")
    created = await DBInstanceApplicationService(session).create(
        CreateDBInstanceCommand(
            agent_id=agent_id,
            mode=command.mode,
            idempotency_key=command.idempotency_key,
            name=command.name,
            db_type=command.db_type,
        )
    )
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    mysql = AsyncMock()
    worker = DedicatedPoolWorker(
        factory,
        TenantProvisioningConfig(),
        AsyncMock(),
        mysql,
        worker_id="dedicated-recheck",
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True

    session.expire_all()
    resource = await session.get(DBInstanceResource, created.resource_id)
    member = await session.get(DedicatedPoolMember, member_id)
    assert resource.status == DBInstanceStatus.CREATING
    assert resource.allocated_instance_id != member.instance_id
    assert member.status == DedicatedMemberStatus.AVAILABLE
    assert member.allocated_resource_id is None
    assert member.readiness_status == ReadinessStatus.FRESH
    mysql.verify.assert_awaited_once()
    worker._provisioner.advance.assert_awaited_once()


async def test_multitenant_dispatcher_never_claims_dedicated_create(
    dedicated_context,
):
    session, agent_id, _pool_id, member_id = dedicated_context
    member = await session.get(DedicatedPoolMember, member_id)
    member.readiness_status = ReadinessStatus.STALE
    await session.commit()
    command = _command("dedicated-worker-routing")
    await DBInstanceApplicationService(session).create(
        CreateDBInstanceCommand(
            agent_id=agent_id,
            mode=command.mode,
            idempotency_key=command.idempotency_key,
            name=command.name,
            db_type=command.db_type,
        )
    )
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    worker = DBInstanceResourceWorker(
        factory,
        TenantProvisioningConfig(),
        "multitenant-worker",
    )

    assert await worker.claim_one() is None


async def test_physical_deficit_reserves_allocated_preparing_member_for_request(
    dedicated_context,
):
    session, agent_id, pool_id, member_id = dedicated_context
    member = await session.get(DedicatedPoolMember, member_id)
    member.status = DedicatedMemberStatus.QUARANTINED
    member.readiness_status = ReadinessStatus.STALE
    await session.commit()

    command = _command("dedicated-cold")
    created = await DBInstanceApplicationService(session).create(
        CreateDBInstanceCommand(
            agent_id=agent_id,
            mode=command.mode,
            idempotency_key=command.idempotency_key,
            name=command.name,
            db_type=command.db_type,
        )
    )

    assert created.status == "CREATING"
    members = list(
        (
            await session.execute(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.pool_id == pool_id
                )
            )
        )
        .scalars()
        .all()
    )
    preparing = [
        value
        for value in members
        if value.status == DedicatedMemberStatus.ALLOCATED_PREPARING
    ]
    assert len(preparing) == 1
    assert preparing[0].allocated_resource_id == created.resource_id
    assert preparing[0].instance.allocation_mode == (
        AllocationMode.DEDICATED_POOL
    )
    resource = await session.get(DBInstanceResource, created.resource_id)
    assert resource.status == DBInstanceStatus.CREATING
    assert resource.allocated_instance_id == preparing[0].instance_id


async def test_deterministic_primary_blocker_falls_back_before_intent(
    dedicated_context,
):
    session, agent_id, primary_pool_id, primary_member_id = dedicated_context
    primary_pool = await session.get(DedicatedPool, primary_pool_id)
    primary_pool.purchase_profile_id = None
    primary_pool.purchase_profile_revision = None
    primary_pool.storage_type = None
    primary_pool.purchase_config_json = "{}"
    primary_member = await session.get(DedicatedPoolMember, primary_member_id)
    primary_member.status = DedicatedMemberStatus.QUARANTINED
    revision = primary_pool.permission_template_revision
    creator_id = await session.scalar(
        select(AgentProvisioningBinding.created_by_user_id).where(
            AgentProvisioningBinding.agent_id == agent_id,
            AgentProvisioningBinding.routing_order == 0,
        )
    )
    assert creator_id is not None
    fallback_pool = DedicatedPool(
        name="dedicated-fallback",
        target_size=1,
        max_total_members=3,
        max_member_purchases_per_hour=3,
        max_create_requests_per_agent_per_hour=10,
        max_delete_requests_per_agent_per_hour=10,
        purchase_config_json=json.dumps(
            build_purchase_config(storage_type="essdpl1"),
            separators=(",", ":"),
            sort_keys=True,
        ),
        purchase_profile_id=PROFILE_ID,
        purchase_profile_revision=PROFILE_REVISION,
        storage_type="essdpl1",
        region_id="cn-hangzhou",
        vpc_id="vpc-fallback",
        vswitch_id="vsw-fallback",
        permission_template_revision=revision,
    )
    fallback_backend = ProvisioningBackend(
        backend_type=ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=fallback_pool,
        permission_template_revision=revision,
        max_active_resources=10,
    )
    session.add(fallback_backend)
    await session.flush()
    session.add_all(
        [
            ProvisioningBackendHealth(
                backend=fallback_backend,
                healthy=True,
                checked_at=utc_now(),
            ),
            AgentProvisioningBinding(
                agent_id=agent_id,
                backend=fallback_backend,
                routing_order=1,
                created_by_user_id=creator_id,
            ),
        ]
    )
    await session.commit()
    fallback_backend_id = fallback_backend.id

    command = _command("dedicated-fallback")
    created = await DBInstanceApplicationService(session).create(
        CreateDBInstanceCommand(
            agent_id=agent_id,
            mode=command.mode,
            idempotency_key=command.idempotency_key,
            name=command.name,
            db_type=command.db_type,
        )
    )

    resource = await session.get(DBInstanceResource, created.resource_id)
    assert resource.backend_id == fallback_backend_id
    assert resource.allocated_instance_id is not None


async def test_fallback_hot_capacity_precedes_primary_cold_purchase(
    dedicated_context,
):
    session, agent_id, primary_pool_id, primary_member_id = dedicated_context
    primary_pool = await session.get(DedicatedPool, primary_pool_id)
    primary_member = await session.get(DedicatedPoolMember, primary_member_id)
    primary_member.status = DedicatedMemberStatus.QUARANTINED
    revision = primary_pool.permission_template_revision
    creator_id = await session.scalar(
        select(AgentProvisioningBinding.created_by_user_id).where(
            AgentProvisioningBinding.agent_id == agent_id,
            AgentProvisioningBinding.routing_order == 0,
        )
    )
    fallback_pool = DedicatedPool(
        name="dedicated-hot-fallback",
        target_size=1,
        max_total_members=3,
        max_member_purchases_per_hour=3,
        max_create_requests_per_agent_per_hour=10,
        max_delete_requests_per_agent_per_hour=10,
        purchase_config_json=json.dumps(
            build_purchase_config(storage_type="essdpl1"),
            separators=(",", ":"),
            sort_keys=True,
        ),
        purchase_profile_id=PROFILE_ID,
        purchase_profile_revision=PROFILE_REVISION,
        storage_type="essdpl1",
        region_id="cn-hangzhou",
        vpc_id="vpc-hot-fallback",
        vswitch_id="vsw-hot-fallback",
        permission_template_revision=revision,
    )
    fallback_backend = ProvisioningBackend(
        backend_type=ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=fallback_pool,
        permission_template_revision=revision,
        max_active_resources=10,
    )
    fallback_instance = Instance(
        cluster_id="pc-hot-fallback",
        name="Hot fallback member",
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.DEDICATED_POOL,
        status=InstanceStatus.ACTIVE,
        region="cn-hangzhou",
        host="hot-fallback.internal",
        port=3306,
    )
    session.add_all([fallback_backend, fallback_instance])
    await session.flush()
    fallback_member = DedicatedPoolMember(
        pool=fallback_pool,
        instance=fallback_instance,
        status=DedicatedMemberStatus.AVAILABLE,
        readiness_status=ReadinessStatus.FRESH,
        last_ready_verified_at=utc_now(),
        host="hot-fallback.internal",
        port=3306,
        database_name="agentic",
        sandbox_username_ciphertext=encrypt("agentic"),
        sandbox_password_ciphertext=encrypt("fallback-secret"),
        permission_template_revision=revision,
        permission_snapshot_json=primary_member.permission_snapshot_json,
    )
    session.add_all(
        [
            fallback_member,
            ProvisioningBackendHealth(
                backend=fallback_backend,
                healthy=True,
                checked_at=utc_now(),
            ),
            AgentProvisioningBinding(
                agent_id=agent_id,
                backend=fallback_backend,
                routing_order=1,
                created_by_user_id=creator_id,
            ),
        ]
    )
    await session.commit()
    fallback_backend_id = fallback_backend.id
    fallback_member_id = fallback_member.id

    command = _command("dedicated-hot-fallback")
    created = await DBInstanceApplicationService(session).create(
        CreateDBInstanceCommand(
            agent_id=agent_id,
            mode=command.mode,
            idempotency_key=command.idempotency_key,
            name=command.name,
            db_type=command.db_type,
        )
    )

    resource = await session.get(DBInstanceResource, created.resource_id)
    fallback_member = await session.get(
        DedicatedPoolMember, fallback_member_id
    )
    assert created.status == "READY"
    assert resource.backend_id == fallback_backend_id
    assert fallback_member.allocated_resource_id == resource.id
    primary_members = list(
        (
            await session.scalars(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.pool_id == primary_pool_id
                )
            )
        ).all()
    )
    assert len(primary_members) == 1
