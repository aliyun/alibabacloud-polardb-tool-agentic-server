from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import server.core.dedicated_pool_repository as dedicated_repository
from server.core.dedicated_purchase_profile import (
    PROFILE_ID,
    PROFILE_REVISION,
    build_purchase_config,
)
from server.core.dedicated_pool_repository import (
    PoolCapacityLimitReached,
    capacity_snapshot,
    claim_fresh_member,
    reserve_member_purchase,
)
from server.models import (
    AllocationMode,
    Base,
    Agent,
    DBInstanceResource,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    Instance,
    InstanceStatus,
    InstanceTopology,
    PermissionTemplate,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendType,
    ProvisioningMode,
    ReadinessStatus,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


async def test_capacity_snapshot_counts_non_deleted_members_as_billable(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    template = PermissionTemplate(name="capacity-template")
    revision = PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    pool = DedicatedPool(
        name="capacity-pool",
        target_size=1,
        max_total_members=10,
        max_member_purchases_per_hour=2,
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
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )
    session.add(pool)
    await session.flush()
    statuses = [
        DedicatedMemberStatus.AVAILABLE,
        DedicatedMemberStatus.REPLENISHING,
        DedicatedMemberStatus.COOLING_DOWN,
        DedicatedMemberStatus.QUARANTINED,
        DedicatedMemberStatus.DELETING,
        DedicatedMemberStatus.DELETED,
    ]
    for index, status in enumerate(statuses):
        instance = Instance(
            cluster_id=f"capacity-{index}",
            name=f"Capacity {index}",
            topology=InstanceTopology.SINGLE_TENANT,
            status=InstanceStatus.ACTIVE,
        )
        session.add(instance)
        await session.flush()
        session.add(
            DedicatedPoolMember(
                pool_id=pool.id,
                instance_id=instance.id,
                status=status,
                readiness_status=ReadinessStatus.FRESH,
                last_ready_verified_at=now - timedelta(seconds=1),
            )
        )
    await session.commit()

    snapshot = await capacity_snapshot(
        session,
        pool_id=pool.id,
        now=now,
    )
    assert snapshot.allocatable == 1
    assert snapshot.planning == 2
    assert snapshot.billable_total == 5


async def test_fresh_member_can_only_be_claimed_once(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    template = PermissionTemplate(name="claim-template")
    revision = PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    pool = DedicatedPool(
        name="claim-pool",
        target_size=1,
        max_total_members=3,
        max_member_purchases_per_hour=2,
        max_create_requests_per_agent_per_hour=10,
        max_delete_requests_per_agent_per_hour=10,
        purchase_config_json="{}",
        region_id="cn-hangzhou",
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )
    backend = ProvisioningBackend(
        backend_type=ProvisioningBackendType.DEDICATED_POOL,
        dedicated_pool=pool,
        max_active_resources=10,
    )
    agent = Agent(name="claim-agent")
    instance = Instance(
        cluster_id="claim-instance",
        name="Claim instance",
        topology=InstanceTopology.SINGLE_TENANT,
        status=InstanceStatus.ACTIVE,
    )
    session.add_all([backend, agent, instance])
    await session.flush()
    resources = [
        DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token=f"claim-{index}",
            request_fingerprint=f"fingerprint-{index}",
            fingerprint_version=2,
            provisioning_mode=ProvisioningMode.DEDICATED,
        )
        for index in range(2)
    ]
    member = DedicatedPoolMember(
        pool_id=pool.id,
        instance_id=instance.id,
        status=DedicatedMemberStatus.AVAILABLE,
        readiness_status=ReadinessStatus.FRESH,
        last_ready_verified_at=now,
    )
    session.add_all([*resources, member])
    await session.commit()

    claimed = await claim_fresh_member(
        session,
        pool_id=pool.id,
        resource_id=resources[0].id,
        now=now,
    )
    await session.commit()
    assert claimed is not None
    assert claimed.status == DedicatedMemberStatus.ALLOCATED_PREPARING
    assert claimed.allocated_resource_id == resources[0].id
    assert resources[0].allocated_instance_id == instance.id

    async with AsyncSession(session.bind, expire_on_commit=False) as contender:
        assert await claim_fresh_member(
            contender,
            pool_id=pool.id,
            resource_id=resources[1].id,
            now=now,
        ) is None


async def test_purchase_guard_counts_cooling_member_against_hard_limit(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    template = PermissionTemplate(name="guard-template")
    revision = PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    pool = DedicatedPool(
        name="guard-pool",
        target_size=1,
        max_total_members=1,
        max_member_purchases_per_hour=1,
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
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )
    instance = Instance(
        cluster_id="guard-instance",
        name="Guard instance",
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.DEDICATED_POOL,
        status=InstanceStatus.ACTIVE,
    )
    session.add_all([pool, instance])
    await session.flush()
    session.add(
        DedicatedPoolMember(
            pool_id=pool.id,
            instance_id=instance.id,
            status=DedicatedMemberStatus.COOLING_DOWN,
        )
    )
    await session.commit()

    with pytest.raises(PoolCapacityLimitReached):
        await reserve_member_purchase(
            session, pool_id=pool.id, now=now, intent="prewarm"
        )


async def test_purchase_guard_rejects_legacy_profile(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    template = PermissionTemplate(name="legacy-profile-template")
    revision = PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    pool = DedicatedPool(
        name="legacy-profile-pool",
        target_size=1,
        max_total_members=3,
        max_member_purchases_per_hour=2,
        max_create_requests_per_agent_per_hour=10,
        max_delete_requests_per_agent_per_hour=10,
        purchase_config_json="{}",
        region_id="cn-hangzhou",
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )
    session.add(pool)
    await session.commit()

    with pytest.raises(dedicated_repository.PurchaseProfileUpgradeRequired):
        await reserve_member_purchase(
            session, pool_id=pool.id, now=now, intent="prewarm"
        )


async def test_purchase_guard_blocks_unlinked_legacy_physical_instance(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    template = PermissionTemplate(name="legacy-instance-template")
    revision = PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    pool = DedicatedPool(
        name="legacy-instance-guard-pool",
        target_size=1,
        max_total_members=3,
        max_member_purchases_per_hour=2,
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
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )
    legacy = Instance(
        cluster_id="pc-unlinked-legacy",
        name="Unlinked legacy pool instance",
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.POOLED,
        status=InstanceStatus.ACTIVE,
    )
    session.add_all([pool, legacy])
    await session.commit()

    with pytest.raises(RuntimeError, match="LEGACY_POOL_INSTANCE_PRESENT"):
        await reserve_member_purchase(
            session, pool_id=pool.id, now=now, intent="prewarm"
        )


async def test_request_purchase_does_not_use_spare_target_guard(session):
    now = datetime(2026, 8, 10, 8, 23, tzinfo=timezone.utc)
    template = PermissionTemplate(name="request-intent-template")
    revision = PermissionTemplateRevision(
        template=template,
        revision=1,
        privileges_json='["SELECT"]',
    )
    pool = DedicatedPool(
        name="request-intent-pool",
        target_size=0,
        max_total_members=1,
        max_member_purchases_per_hour=1,
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
        vpc_id="vpc-test",
        vswitch_id="vsw-test",
        permission_template_revision=revision,
    )
    session.add(pool)
    await session.commit()

    assert await reserve_member_purchase(
        session, pool_id=pool.id, now=now, intent="request"
    ) is True
