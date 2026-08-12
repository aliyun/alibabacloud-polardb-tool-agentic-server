from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import OpenAPIError
from server.config import reset_config
from server.core.crypto import decrypt
from server.core.dedicated_pool_provisioner import DedicatedPoolProvisioner
from server.models import (
    Agent,
    AllocationMode,
    Base,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    DedicatedPreparationStep,
    DBInstanceResource,
    DBInstanceStatus,
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


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_super_once = False
        self.account_statuses: dict[str, str | None] = {}
        self.during_purchase: Callable[[], Awaitable[None]] | None = None

    async def create_dedicated_cluster(self, params, **kwargs):
        self.calls.append(("purchase", dict(params), kwargs))
        if self.during_purchase is not None:
            await self.during_purchase()
        return {
            "cluster_id": "pc-prewarmed",
            "agentic_db_cluster_id": "pagc-prewarmed",
            "request_id": "request-safe-1",
        }

    async def describe_cluster_attribute(self, cluster_id):
        self.calls.append(("describe", cluster_id))
        return {"status": "Running"}

    async def describe_endpoints(self, cluster_id):
        self.calls.append(("endpoints", cluster_id))
        return {
            "items": [
                {
                    "endpoint_type": "Primary",
                    "address_items": [
                        {
                            "connection_string": "prewarm.internal",
                            "port": "3306",
                            "net_type": "Private",
                        }
                    ],
                }
            ]
        }

    async def create_account(
        self,
        cluster_id,
        account_name,
        password,
        account_type="Normal",
    ):
        self.calls.append(
            ("account", cluster_id, account_name, password, account_type)
        )
        if account_type == "Super" and self.fail_super_once:
            self.fail_super_once = False
            raise RuntimeError("safe injected failure")
        self.account_statuses.setdefault(account_name, "Available")
        return {"account_name": account_name}

    async def describe_account(self, cluster_id, account_name):
        self.calls.append(("describe_account", cluster_id, account_name))
        status = self.account_statuses.get(account_name)
        return {"account_name": account_name, "status": status} if status else None

    async def create_database(
        self,
        cluster_id,
        db_name,
        account_name=None,
        **_kwargs,
    ):
        self.calls.append(
            ("database", cluster_id, db_name, account_name)
        )


class FakeDedicatedMySQL:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def apply_permissions(self, member):
        self.calls.append(("grant", member.id))

    async def verify(self, member):
        self.calls.append(("verify", member.id))


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
async def provisioner_context(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/provisioner.db",
        connect_args={"timeout": 0.1},
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        template = PermissionTemplate(name="prewarm-template")
        revision = PermissionTemplateRevision(
            template=template,
            revision=1,
            privileges_json='["SELECT","INSERT"]',
        )
        pool = DedicatedPool(
            name="prewarm-pool",
            target_size=1,
            max_total_members=3,
            max_member_purchases_per_hour=2,
            max_create_requests_per_agent_per_hour=10,
            max_delete_requests_per_agent_per_hour=10,
            purchase_config_json='{"db_node_class":"test-class"}',
            region_id="cn-hangzhou",
            vpc_id="vpc-test",
            vswitch_id="vsw-test",
            permission_template_revision=revision,
        )
        instance = Instance(
            cluster_id="pending-prewarm",
            name="Prewarm member",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.DEDICATED_POOL,
            status=InstanceStatus.CREATING,
        )
        session.add_all([pool, instance])
        await session.flush()
        member = DedicatedPoolMember(
            pool_id=pool.id,
            instance_id=instance.id,
            status=DedicatedMemberStatus.REPLENISHING,
            readiness_status=ReadinessStatus.STALE,
            preparation_step=DedicatedPreparationStep.PENDING,
        )
        session.add(member)
        await session.commit()
        member_id = member.id
    client = FakeClient()
    mysql = FakeDedicatedMySQL()
    yield factory, member_id, client, mysql
    await engine.dispose()


async def test_prewarmer_resumes_with_same_encrypted_lifecycle_secret(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    provisioner = DedicatedPoolProvisioner(factory, client, mysql)

    for _ in range(5):
        await provisioner.advance(member_id)
    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.preparation_step == (
            DedicatedPreparationStep.LIFECYCLE_ACCOUNT_STORED
        )
        lifecycle_ciphertext = member.lifecycle_password_ciphertext
        lifecycle_password = decrypt(lifecycle_ciphertext)

    client.fail_super_once = True
    with pytest.raises(RuntimeError, match="safe injected failure"):
        await provisioner.advance(member_id)
    await provisioner.advance(member_id)
    super_calls = [
        call
        for call in client.calls
        if call[0] == "account" and call[4] == "Super"
    ]
    assert len(super_calls) == 2
    assert {call[3] for call in super_calls} == {lifecycle_password}
    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.lifecycle_password_ciphertext == lifecycle_ciphertext
        assert member.failure_reason == "DEDICATED_PREWARM_RUNTIMEERROR"
        assert "safe injected failure" not in member.failure_reason


async def test_prewarmer_persists_cloud_request_id_from_purchase_failure(
    provisioner_context, monkeypatch,
):
    factory, member_id, client, mysql = provisioner_context
    provisioner = DedicatedPoolProvisioner(factory, client, mysql)
    occurred_at = datetime(2026, 8, 11, 14, 19, 19, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "server.core.dedicated_pool_provisioner._utcnow",
        lambda: occurred_at,
    )

    await provisioner.advance(member_id)

    async def fail_purchase(*_args, **_kwargs):
        raise OpenAPIError(
            "InvalidVpcId.NotFound",
            "VPC does not exist",
            request_id="request-vpc-not-found",
            operation="CreateDBCluster",
        )

    client.create_dedicated_cluster = fail_purchase
    with pytest.raises(OpenAPIError, match="InvalidVpcId.NotFound"):
        await provisioner.advance(member_id)

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.cloud_request_id == "request-vpc-not-found"
        assert json.loads(member.failure_reason) == {
            "code": "DEDICATED_PREWARM_INVALIDVPCID_NOTFOUND",
            "detail": "VPC does not exist",
            "occurred_at": "2026-08-11T14:19:19+00:00",
            "operation": "CreateDBCluster",
        }


async def test_prewarmer_normalizes_legacy_pool_network_before_purchase(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        member.pool.region_id = " cn-beijing "
        member.pool.zone_id = " cn-beijing-k "
        member.pool.vpc_id = " vpc-legacy "
        member.pool.vswitch_id = " vsw-legacy "
        await session.commit()

    provisioner = DedicatedPoolProvisioner(factory, client, mysql)
    await provisioner.advance(member_id)
    await provisioner.advance(member_id)

    purchase = client.calls[0]
    assert purchase[0] == "purchase"
    assert purchase[1]["region_id"] == "cn-beijing"
    assert purchase[1]["zone_id"] == "cn-beijing-k"
    assert purchase[1]["vpc_id"] == "vpc-legacy"
    assert purchase[1]["vswitch_id"] == "vsw-legacy"


async def test_prewarmer_releases_database_transaction_during_purchase(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    provisioner = DedicatedPoolProvisioner(factory, client, mysql)
    await provisioner.advance(member_id)

    async def write_while_openapi_is_running() -> None:
        async with factory() as session:
            session.add(PermissionTemplate(name="concurrent-template"))
            await session.commit()

    client.during_purchase = write_while_openapi_is_running
    await provisioner.advance(member_id)

    async with factory() as session:
        assert await session.scalar(
            select(PermissionTemplate).where(
                PermissionTemplate.name == "concurrent-template"
            )
        ) is not None


async def test_prewarmer_reaches_available_only_after_grant_and_verify(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    provisioner = DedicatedPoolProvisioner(factory, client, mysql)

    for _ in range(12):
        await provisioner.advance(member_id)

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.status == DedicatedMemberStatus.AVAILABLE
        assert member.readiness_status == ReadinessStatus.FRESH
        assert member.last_ready_verified_at is not None
        assert member.host == "prewarm.internal"
        assert member.port == 3306
        assert member.database_name == "agentic"
        assert member.permission_snapshot_json is not None
        assert member.preparation_step == DedicatedPreparationStep.VERIFIED
        assert member.lifecycle_username_ciphertext != "pas_lifecycle"
        assert member.sandbox_username_ciphertext != "agentic"
        assert member.instance.allocation_mode == AllocationMode.DEDICATED_POOL

    assert [call[0] for call in client.calls] == [
        "purchase",
        "describe",
        "endpoints",
        "account",
        "describe_account",
        "account",
        "describe_account",
        "database",
    ]
    assert mysql.calls == [("grant", member_id), ("verify", member_id)]


async def test_prewarmer_pauses_after_openapi_without_data_plane_access(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    provisioner = DedicatedPoolProvisioner(factory, client, mysql)

    for _ in range(10):
        await provisioner.advance(member_id, allow_data_plane=False)

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.preparation_step == DedicatedPreparationStep.OPENAPI_READY
        assert member.status == DedicatedMemberStatus.REPLENISHING
        assert member.readiness_status == ReadinessStatus.STALE
        assert member.instance.status == InstanceStatus.CREATING

    database_call = next(call for call in client.calls if call[0] == "database")
    assert database_call[3] is None
    assert mysql.calls == []

    await provisioner.advance(member_id, allow_data_plane=False)
    assert mysql.calls == []

    await provisioner.advance(member_id, allow_data_plane=True)
    assert mysql.calls == [("grant", member_id)]


async def test_prewarmer_waits_for_each_account_before_using_it(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    provisioner = DedicatedPoolProvisioner(factory, client, mysql)

    for _ in range(6):
        await provisioner.advance(member_id)

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        lifecycle_name = decrypt(member.lifecycle_username_ciphertext)
        assert member.preparation_step == (
            DedicatedPreparationStep.LIFECYCLE_ACCOUNT_CREATED
        )

    client.account_statuses[lifecycle_name] = "Creating"
    await provisioner.advance(member_id)

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        assert member.preparation_step == (
            DedicatedPreparationStep.LIFECYCLE_ACCOUNT_CREATED
        )
        assert member.sandbox_username_ciphertext is None

    client.account_statuses[lifecycle_name] = "Available"
    await provisioner.advance(member_id)
    await provisioner.advance(member_id)

    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        assert member is not None
        agent_name = decrypt(member.sandbox_username_ciphertext)
        assert member.preparation_step == (
            DedicatedPreparationStep.SANDBOX_ACCOUNT_CREATED
        )

    client.account_statuses[agent_name] = "Creating"
    await provisioner.advance(member_id)
    assert not any(call[0] == "database" for call in client.calls)

    client.account_statuses[agent_name] = "Available"
    await provisioner.advance(member_id)

    database_call = next(call for call in client.calls if call[0] == "database")
    assert database_call[3] is None


async def test_cold_prewarmer_finishes_reserved_resource_ready(
    provisioner_context,
):
    factory, member_id, client, mysql = provisioner_context
    async with factory() as session:
        member = await session.get(DedicatedPoolMember, member_id)
        agent = Agent(name="cold-prewarm-agent")
        backend = ProvisioningBackend(
            backend_type=ProvisioningBackendType.DEDICATED_POOL,
            dedicated_pool_id=member.pool_id,
            max_active_resources=10,
        )
        session.add_all([agent, backend])
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="cold-prewarm",
            request_fingerprint="c" * 64,
            provisioning_mode=ProvisioningMode.DEDICATED,
            allocated_instance_id=member.instance_id,
        )
        session.add(resource)
        await session.flush()
        member.allocated_resource_id = resource.id
        await session.commit()
        resource_id = resource.id

    provisioner = DedicatedPoolProvisioner(factory, client, mysql)
    for _ in range(12):
        await provisioner.advance(member_id)

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        member = await session.get(DedicatedPoolMember, member_id)
        assert resource.status == DBInstanceStatus.READY
        assert resource.database_name == "agentic"
        assert len(resource.credentials) == 1
        assert member.status == DedicatedMemberStatus.ALLOCATED
