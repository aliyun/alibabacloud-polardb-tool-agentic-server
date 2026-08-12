from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig, reset_config
from server.core.adapter_registry import AdapterRegistry
from server.core.crypto import encrypt
from server.core.db_instance_dispatcher import DBInstanceDispatcher
from server.core.db_instance_service import (
    delete_db_instance_resource,
    restore_db_instance_resource,
)
from server.models import (
    Agent,
    Base,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    DeleteLifecycleStep,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    LeaseProvisioningStep,
    ProvisioningBackend,
    ProvisioningCapacity,
    User,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

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
async def lifecycle_context(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lifecycle.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        creator = User(external_id="lifecycle-admin", display_name="Admin")
        agent = Agent(name="lifecycle-agent", max_active_resources=10)
        instance = Instance(
            cluster_id="pc-lifecycle",
            name="Lifecycle backend",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            status=InstanceStatus.ACTIVE,
            host="lifecycle.internal",
            port=3306,
        )
        session.add_all([creator, agent, instance])
        await session.flush()
        admin = InstanceCredential(
            instance_id=instance.id,
            name="admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("root-secret"),
            created_by_user_id=creator.id,
        )
        session.add(admin)
        await session.flush()
        backend = ProvisioningBackend(
            instance_id=instance.id,
            admin_credential_id=admin.id,
            max_active_resources=10,
        )
        session.add(backend)
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="lifecycle-resource",
            request_fingerprint="d" * 64,
            status=DBInstanceStatus.READY,
            tenant_name="t123456789",
            resource_config_name="rc_t123456789",
            database_name="agentic@t123456789",
            provisioning_step=LeaseProvisioningStep.VERIFIED,
            permission_snapshot_json=(
                '{"grant_option":false,"legacy":false,'
                '"privileges":["SELECT"],"scope":"tenant"}'
            ),
        )
        session.add(resource)
        await session.flush()
        credential = InstanceCredential(
            resource_id=resource.id,
            name="resource-access",
            purpose=CredentialPurpose.RESOURCE_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("agentic@t123456789"),
            password_ciphertext=encrypt("original-secret"),
            database_name=resource.database_name,
        )
        session.add_all(
            [
                credential,
                ProvisioningCapacity(
                    scope_type="agent", scope_id=agent.id, active_count=1
                ),
                ProvisioningCapacity(
                    scope_type="backend", scope_id=backend.id, active_count=1
                ),
            ]
        )
        await session.commit()
        resource_id = resource.id
        agent_id = agent.id
        ciphertext = credential.password_ciphertext

    adapter = AsyncMock()

    async def disconnect(resource):
        resource.delete_step = {
            DeleteLifecycleStep.PENDING: DeleteLifecycleStep.ACCOUNT_LOCKED,
            DeleteLifecycleStep.ACCOUNT_LOCKED: (
                DeleteLifecycleStep.SESSIONS_TERMINATED
            ),
            DeleteLifecycleStep.SESSIONS_TERMINATED: (
                DeleteLifecycleStep.DISCONNECTED
            ),
        }[resource.delete_step]

    adapter.disconnect.side_effect = disconnect
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
        worker_max_retries=1,
        delete_cooldown_duration_hours=24,
    )
    yield factory, resource_id, agent_id, ciphertext, adapter, registry, clock, config
    await engine.dispose()


def _dispatcher(context):
    factory, _rid, _aid, _cipher, _adapter, registry, clock, config = context
    return DBInstanceDispatcher(
        factory, config, registry, worker_id="lifecycle-worker", clock=clock
    )


async def _load(context):
    factory, resource_id, *_rest = context
    async with factory() as session:
        return await session.get(DBInstanceResource, resource_id)


async def test_disconnect_proof_starts_cooldown_from_delayed_completion(
    lifecycle_context,
):
    factory, resource_id, agent_id, _cipher, adapter, _registry, clock, _config = (
        lifecycle_context
    )
    async with factory() as session:
        deleted = await delete_db_instance_resource(session, agent_id, resource_id)
        assert deleted.cooldown_until is None
        assert deleted.disconnected_at is None
        assert deleted.credentials[0].status == CredentialStatus.REVOKED
        assert deleted.credentials[0].password_ciphertext is not None
    clock.advance(hours=3)

    assert await _dispatcher(lifecycle_context).run_once() is True

    stored = await _load(lifecycle_context)
    assert stored.status == DBInstanceStatus.COOLING_DOWN
    assert stored.delete_step == DeleteLifecycleStep.COOLING_DOWN
    assert stored.disconnected_at.replace(tzinfo=timezone.utc) == clock.value
    assert stored.cooldown_until.replace(tzinfo=timezone.utc) == (
        clock.value + timedelta(hours=24)
    )
    assert adapter.disconnect.await_count == 3


async def test_disconnect_exhaustion_never_creates_cooldown_deadline(
    lifecycle_context,
):
    factory, resource_id, agent_id, _cipher, adapter, _registry, clock, _config = (
        lifecycle_context
    )
    async with factory() as session:
        await delete_db_instance_resource(session, agent_id, resource_id)
    adapter.disconnect.side_effect = RuntimeError("raw connection detail")
    dispatcher = _dispatcher(lifecycle_context)

    assert await dispatcher.run_once() is True
    clock.advance(seconds=1)
    assert await dispatcher.run_once() is True

    stored = await _load(lifecycle_context)
    assert stored.status == DBInstanceStatus.DELETE_FAILED
    assert stored.cooldown_until is None
    assert "raw connection detail" not in stored.failure_reason


async def test_restore_reuses_original_credential_before_cleanup(
    lifecycle_context,
):
    factory, resource_id, agent_id, ciphertext, adapter, _registry, _clock, _config = (
        lifecycle_context
    )
    async with factory() as session:
        await delete_db_instance_resource(session, agent_id, resource_id)
    await _dispatcher(lifecycle_context).run_once()
    async with factory() as session:
        await restore_db_instance_resource(session, resource_id)

    assert await _dispatcher(lifecycle_context).run_once() is True

    stored = await _load(lifecycle_context)
    assert stored.status == DBInstanceStatus.READY
    assert stored.credentials[0].status == CredentialStatus.ACTIVE
    assert stored.credentials[0].password_ciphertext == ciphertext
    assert stored.cooldown_until is None
    adapter.restore.assert_awaited_once()


async def test_failed_restore_from_cooling_keeps_deadline_and_non_ready_state(
    lifecycle_context,
):
    factory, resource_id, agent_id, _cipher, adapter, _registry, _clock, _config = (
        lifecycle_context
    )
    async with factory() as session:
        await delete_db_instance_resource(session, agent_id, resource_id)
    await _dispatcher(lifecycle_context).run_once()
    before = await _load(lifecycle_context)
    deadline = before.cooldown_until
    async with factory() as session:
        await restore_db_instance_resource(session, resource_id)
    adapter.restore.side_effect = RuntimeError("secret endpoint")

    assert await _dispatcher(lifecycle_context).run_once() is True

    stored = await _load(lifecycle_context)
    assert stored.status == DBInstanceStatus.COOLING_DOWN
    assert stored.cooldown_until == deadline
    assert "secret endpoint" not in stored.restore_failure_reason
