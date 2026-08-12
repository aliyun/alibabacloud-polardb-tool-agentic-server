from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig, reset_config
from server.core.crypto import encrypt
from server.core.db_instance_service import (
    DBInstanceServiceError,
    delete_db_instance_resource,
    restore_db_instance_resource,
)
from server.core.dedicated_pool_worker import DedicatedPoolWorker
from server.models import (
    Agent,
    AllocationMode,
    Base,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    DeleteLifecycleStep,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    PermissionTemplate,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendType,
    ProvisioningCapacity,
    ProvisioningMode,
    ReadinessStatus,
    ReclaimPolicy,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


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
async def dedicated_lifecycle_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/dedicated-life.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        agent = Agent(name="dedicated-life-agent", max_active_resources=10)
        template = PermissionTemplate(name="dedicated-life-template")
        revision = PermissionTemplateRevision(
            template=template,
            revision=1,
            privileges_json='["SELECT"]',
        )
        pool = DedicatedPool(
            name="dedicated-life-pool",
            target_size=1,
            max_total_members=3,
            max_member_purchases_per_hour=3,
            max_create_requests_per_agent_per_hour=10,
            max_delete_requests_per_agent_per_hour=10,
            purchase_config_json="{}",
            region_id="cn-hangzhou",
            vpc_id="vpc-life",
            vswitch_id="vsw-life",
            delete_cooldown_duration_hours=1,
            reclaim_policy=ReclaimPolicy.DESTROY,
            permission_template_revision=revision,
        )
        backend = ProvisioningBackend(
            backend_type=ProvisioningBackendType.DEDICATED_POOL,
            dedicated_pool=pool,
            permission_template_revision=revision,
            max_active_resources=10,
        )
        instance = Instance(
            cluster_id="pc-dedicated-life",
            name="Dedicated life member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.DEDICATED_POOL,
            status=InstanceStatus.ACTIVE,
            host="dedicated-life.internal",
            port=3306,
        )
        session.add_all([agent, backend, instance])
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="dedicated-life",
            request_fingerprint="e" * 64,
            provisioning_mode=ProvisioningMode.DEDICATED,
            allocated_instance_id=instance.id,
            status=DBInstanceStatus.READY,
            database_name="agentic",
            permission_template_id=template.id,
            permission_template_revision_id=revision.id,
            permission_snapshot_json=(
                '{"grant_option":false,"legacy":false,'
                '"privileges":["SELECT"],"scope":"global"}'
            ),
        )
        session.add(resource)
        await session.flush()
        credential = InstanceCredential(
            resource_id=resource.id,
            name="resource-access",
            purpose=CredentialPurpose.RESOURCE_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("agentic"),
            password_ciphertext=encrypt("exact-original-secret"),
            database_name="agentic",
        )
        member = DedicatedPoolMember(
            pool=pool,
            instance=instance,
            status=DedicatedMemberStatus.ALLOCATED,
            readiness_status=ReadinessStatus.FRESH,
            allocated_resource=resource,
            host="dedicated-life.internal",
            port=3306,
            database_name="agentic",
            lifecycle_username_ciphertext=encrypt("pas_lifecycle"),
            lifecycle_password_ciphertext=encrypt("lifecycle-secret"),
            sandbox_username_ciphertext=credential.username_ciphertext,
            sandbox_password_ciphertext=credential.password_ciphertext,
            permission_template_revision=revision,
            permission_snapshot_json=resource.permission_snapshot_json,
        )
        session.add_all(
            [
                credential,
                member,
                ProvisioningCapacity(
                    scope_type="agent", scope_id=agent.id, active_count=1
                ),
                ProvisioningCapacity(
                    scope_type="backend", scope_id=backend.id, active_count=1
                ),
            ]
        )
        await session.commit()
        values = resource.id, agent.id, member.id, credential.password_ciphertext

    mysql = AsyncMock()
    provisioner = AsyncMock()
    clock = MutableClock()
    config = TenantProvisioningConfig(
        worker_poll_interval_seconds=1,
        worker_claim_ttl_seconds=10,
        worker_claim_renew_seconds=1,
        worker_max_retries=0,
    )
    yield factory, values, mysql, provisioner, clock, config
    await engine.dispose()


def _worker(context):
    factory, _values, mysql, provisioner, clock, config = context
    return DedicatedPoolWorker(
        factory,
        config,
        provisioner,
        mysql,
        worker_id="dedicated-life-worker",
        clock=clock,
    )


async def _delete(context):
    factory, (resource_id, agent_id, _member_id, _cipher), *_rest = context
    async with factory() as session:
        return await delete_db_instance_resource(session, agent_id, resource_id)


async def _rows(context):
    factory, (resource_id, _agent_id, member_id, _cipher), *_rest = context
    async with factory() as session:
        return (
            await session.get(DBInstanceResource, resource_id),
            await session.get(DedicatedPoolMember, member_id),
        )


async def test_dedicated_delete_disconnects_then_destroys_after_cooldown(
    dedicated_lifecycle_context,
):
    _factory, _values, mysql, provisioner, clock, _config = (
        dedicated_lifecycle_context
    )
    deleted = await _delete(dedicated_lifecycle_context)
    assert deleted.credentials[0].status == CredentialStatus.REVOKED
    assert deleted.cooldown_until is None

    assert await _worker(dedicated_lifecycle_context).run_once() is True
    resource, member = await _rows(dedicated_lifecycle_context)
    assert resource.status == DBInstanceStatus.COOLING_DOWN
    assert member.status == DedicatedMemberStatus.COOLING_DOWN
    mysql.disconnect.assert_awaited_once()

    clock.advance(hours=1)
    assert await _worker(dedicated_lifecycle_context).run_once() is True
    resource, member = await _rows(dedicated_lifecycle_context)
    assert resource.status == DBInstanceStatus.DELETED
    assert member.status == DedicatedMemberStatus.DELETED
    provisioner.delete_cluster.assert_awaited_once_with("pc-dedicated-life")


async def test_dedicated_restore_reuses_exact_credential(
    dedicated_lifecycle_context,
):
    factory, (resource_id, _agent_id, _member_id, ciphertext), mysql, *_rest = (
        dedicated_lifecycle_context
    )
    await _delete(dedicated_lifecycle_context)
    await _worker(dedicated_lifecycle_context).run_once()
    async with factory() as session:
        await restore_db_instance_resource(session, resource_id)

    assert await _worker(dedicated_lifecycle_context).run_once() is True
    resource, member = await _rows(dedicated_lifecycle_context)
    assert resource.status == DBInstanceStatus.READY
    assert resource.credentials[0].status == CredentialStatus.ACTIVE
    assert resource.credentials[0].password_ciphertext == ciphertext
    assert member.status == DedicatedMemberStatus.ALLOCATED
    mysql.restore.assert_awaited_once()


async def test_sanitize_policy_rotates_member_after_cleanup(
    dedicated_lifecycle_context,
):
    factory, (resource_id, _agent_id, _member_id, _cipher), mysql, _provisioner, clock, _config = (
        dedicated_lifecycle_context
    )
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        backend = await session.get(ProvisioningBackend, resource.backend_id)
        backend.dedicated_pool.reclaim_policy = ReclaimPolicy.SANITIZE_AND_REUSE
        await session.commit()
    await _delete(dedicated_lifecycle_context)
    await _worker(dedicated_lifecycle_context).run_once()
    clock.advance(hours=1)

    assert await _worker(dedicated_lifecycle_context).run_once() is True

    resource, member = await _rows(dedicated_lifecycle_context)
    assert resource.status == DBInstanceStatus.DELETED
    assert member.status == DedicatedMemberStatus.REPLENISHING
    assert member.allocated_resource_id is None
    assert member.sandbox_password_ciphertext is None
    mysql.drop_sandbox.assert_awaited_once()


async def test_restore_is_refused_after_irreversible_cleanup_begins(
    dedicated_lifecycle_context,
):
    factory, (resource_id, _agent_id, _member_id, _cipher), *_rest = (
        dedicated_lifecycle_context
    )
    await _delete(dedicated_lifecycle_context)
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.delete_step = DeleteLifecycleStep.PHYSICAL_DESTROY
        await session.commit()
    async with factory() as session:
        with pytest.raises(DBInstanceServiceError, match="irreversible"):
            await restore_db_instance_resource(session, resource_id)
