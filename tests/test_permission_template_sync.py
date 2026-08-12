from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig, reset_config
from server.core.crypto import encrypt
from server.core.dedicated_pool_worker import DedicatedPoolWorker
from server.core.permission_template_service import (
    PermissionSyncConfirmationRequired,
    request_permission_sync,
)
from server.models import (
    Agent,
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
    PermissionSyncMode,
    PermissionSyncStatus,
    PermissionSyncTarget,
    PermissionSyncTargetStatus,
    PermissionTemplate,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendType,
    ProvisioningMode,
    ReadinessStatus,
    User,
)

OLD_SNAPSHOT = (
    '{"grant_option":false,"legacy":false,'
    '"privileges":["SELECT"],"revision_id":"old-revision",'
    '"scope":"global","template_id":"old-template"}'
)


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
async def sync_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/sync.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        admin = User(external_id="sync-admin", display_name="Sync Admin")
        agent = Agent(name="sync-agent")
        template = PermissionTemplate(name="sync-template")
        old_revision = PermissionTemplateRevision(
            template=template,
            revision=1,
            privileges_json='["SELECT"]',
        )
        revision = PermissionTemplateRevision(
            template=template,
            revision=2,
            privileges_json='["SELECT","INSERT"]',
        )
        pool = DedicatedPool(
            name="sync-pool",
            target_size=1,
            max_total_members=2,
            max_member_purchases_per_hour=2,
            max_create_requests_per_agent_per_hour=10,
            max_delete_requests_per_agent_per_hour=10,
            purchase_config_json="{}",
            region_id="cn-hangzhou",
            vpc_id="vpc-sync",
            vswitch_id="vsw-sync",
            permission_template_revision=old_revision,
        )
        backend = ProvisioningBackend(
            backend_type=ProvisioningBackendType.DEDICATED_POOL,
            dedicated_pool=pool,
            permission_template_revision=old_revision,
            max_active_resources=10,
        )
        instance = Instance(
            cluster_id="pc-sync",
            name="Sync member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.POOLED,
            status=InstanceStatus.ACTIVE,
            host="sync.internal",
            port=3306,
        )
        session.add_all([admin, agent, backend, instance, revision])
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="sync-resource",
            request_fingerprint="f" * 64,
            provisioning_mode=ProvisioningMode.DEDICATED,
            allocated_instance_id=instance.id,
            status=DBInstanceStatus.READY,
            database_name="agentic",
            permission_template_id=template.id,
            permission_template_revision_id=old_revision.id,
            permission_snapshot_json=OLD_SNAPSHOT,
        )
        session.add(resource)
        await session.flush()
        member = DedicatedPoolMember(
            pool=pool,
            instance=instance,
            status=DedicatedMemberStatus.ALLOCATED,
            readiness_status=ReadinessStatus.FRESH,
            allocated_resource=resource,
            host="sync.internal",
            port=3306,
            database_name="agentic",
            lifecycle_username_ciphertext=encrypt("lifecycle"),
            lifecycle_password_ciphertext=encrypt("lifecycle-password"),
            sandbox_username_ciphertext=encrypt("agentic"),
            sandbox_password_ciphertext=encrypt("sandbox-password"),
            permission_template_revision=old_revision,
            permission_snapshot_json=OLD_SNAPSHOT,
        )
        session.add(member)
        await session.commit()
        values = admin.id, pool.id, revision.id, member.id, resource.id
    yield factory, values
    await engine.dispose()


async def test_dry_run_is_durable_and_does_not_change_captured_snapshot(
    sync_context,
):
    factory, (admin_id, pool_id, revision_id, member_id, _resource_id) = (
        sync_context
    )
    async with factory() as session:
        job = await request_permission_sync(
            session,
            revision_id=revision_id,
            target_scope="pool",
            target_id=pool_id,
            mode=PermissionSyncMode.DRY_RUN,
            confirmed_by_user_id=admin_id,
        )
        await session.commit()
        assert job.status == PermissionSyncStatus.SUCCEEDED
        assert job.total_count == 1
        target = job.targets[0]
        assert target.status == PermissionSyncTargetStatus.SUCCEEDED
        assert target.change_required is True
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.permission_snapshot_json == OLD_SNAPSHOT


async def test_apply_requires_confirmation(sync_context):
    factory, (_admin_id, pool_id, revision_id, *_rest) = sync_context
    async with factory() as session:
        with pytest.raises(PermissionSyncConfirmationRequired):
            await request_permission_sync(
                session,
                revision_id=revision_id,
                target_scope="pool",
                target_id=pool_id,
                mode=PermissionSyncMode.APPLY,
                confirmed_by_user_id=None,
            )


async def test_worker_applies_and_verifies_each_target(sync_context):
    factory, (admin_id, pool_id, revision_id, member_id, resource_id) = (
        sync_context
    )
    async with factory() as session:
        job = await request_permission_sync(
            session,
            revision_id=revision_id,
            target_scope="pool",
            target_id=pool_id,
            mode=PermissionSyncMode.APPLY,
            confirmed_by_user_id=admin_id,
        )
        await session.commit()
        job_id = job.id

    mysql = AsyncMock()
    worker = DedicatedPoolWorker(
        factory,
        TenantProvisioningConfig(
            worker_poll_interval_seconds=1,
            worker_claim_ttl_seconds=10,
            worker_claim_renew_seconds=1,
        ),
        AsyncMock(),
        mysql,
        worker_id="permission-sync-worker",
        clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert await worker.run_once() is True
    mysql.synchronize_permissions.assert_awaited_once()

    async with factory() as session:
        job = await session.get(type(job), job_id)
        member = await session.get(DedicatedPoolMember, member_id)
        resource = await session.get(DBInstanceResource, resource_id)
        target = await session.get(PermissionSyncTarget, job.targets[0].id)
        assert job.status == PermissionSyncStatus.SUCCEEDED
        assert target.status == PermissionSyncTargetStatus.SUCCEEDED
        assert member.permission_template_revision_id == revision_id
        assert resource.permission_template_revision_id == revision_id
        assert '"INSERT"' in member.permission_snapshot_json
        assert member.permission_snapshot_json == resource.permission_snapshot_json


async def test_worker_records_sanitized_terminal_failure(sync_context):
    factory, (admin_id, pool_id, revision_id, *_rest) = sync_context
    async with factory() as session:
        job = await request_permission_sync(
            session,
            revision_id=revision_id,
            target_scope="pool",
            target_id=pool_id,
            mode=PermissionSyncMode.APPLY,
            confirmed_by_user_id=admin_id,
        )
        await session.commit()
        job_id = job.id
    mysql = AsyncMock()
    mysql.synchronize_permissions.side_effect = RuntimeError(
        "password=must-not-leak raw SQL"
    )
    worker = DedicatedPoolWorker(
        factory,
        TenantProvisioningConfig(
            worker_poll_interval_seconds=1,
            worker_claim_ttl_seconds=10,
            worker_claim_renew_seconds=1,
            worker_max_retries=0,
        ),
        AsyncMock(),
        mysql,
        worker_id="permission-failure-worker",
    )
    assert await worker.run_once() is True
    async with factory() as session:
        job = await session.get(type(job), job_id)
        assert job.status == PermissionSyncStatus.FAILED
        assert job.failure_reason == "PERMISSION_SYNC_FAILED"
        assert "password" not in job.failure_reason
