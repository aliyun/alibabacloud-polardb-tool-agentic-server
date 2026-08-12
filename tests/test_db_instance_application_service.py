from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.config import reset_config
from server.core.crypto import encrypt
from server.core.db_instance_application_service import (
    CreateDBInstanceCommand,
    DBInstanceApplicationService,
)
from server.core.db_instance_service import IdempotencyConflict
from server.models import (
    Agent,
    AgentProvisioningBinding,
    Base,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningMode,
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
async def application_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        creator = User(external_id="application-admin", display_name="Admin")
        agent = Agent(name="application-agent", max_active_resources=10)
        instance = Instance(
            cluster_id="application-cluster",
            name="Application backend",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            status=InstanceStatus.ACTIVE,
            host="application.internal",
            port=3306,
        )
        session.add_all([creator, agent, instance])
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="provisioning-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("admin-secret"),
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
        session.add_all(
            [
                ProvisioningBackendHealth(
                    backend_id=backend.id,
                    healthy=True,
                    checked_at=utc_now(),
                ),
                AgentProvisioningBinding(
                    agent_id=agent.id,
                    backend_id=backend.id,
                    created_by_user_id=creator.id,
                ),
            ]
        )
        agent_id = agent.id
        await session.commit()
        yield session, agent_id
    await engine.dispose()


async def test_mode_is_idempotent_and_failed_replay_is_secret_free(
    application_context,
):
    session, agent_id = application_context
    service = DBInstanceApplicationService(session)
    command = CreateDBInstanceCommand(
        agent_id=agent_id,
        mode=ProvisioningMode.MULTITENANT,
        idempotency_key="application-mode",
        name="Orders",
        db_type="polardb_mysql",
    )

    created = await service.create(command)
    resource = await service.get_resource(
        agent_id=agent_id,
        resource_id=created.resource_id,
    )
    resource.status = DBInstanceStatus.FAILED
    resource.failure_reason = "sanitized failure"
    await session.commit()

    replay = await service.create(command)
    assert replay.resource_id == created.resource_id
    assert replay.status == "FAILED"
    assert replay.connection is None
    assert replay.failure_reason == "sanitized failure"

    with pytest.raises(IdempotencyConflict):
        await service.create(
            CreateDBInstanceCommand(
                agent_id=agent_id,
                mode=ProvisioningMode.DEDICATED,
                idempotency_key=command.idempotency_key,
                name=command.name,
                db_type=command.db_type,
            )
        )


async def test_only_ready_owner_view_contains_complete_connection(
    application_context,
):
    session, agent_id = application_context
    service = DBInstanceApplicationService(session)
    command = CreateDBInstanceCommand(
        agent_id=agent_id,
        mode=ProvisioningMode.MULTITENANT,
        idempotency_key="application-ready",
        name=None,
        db_type="polardb_mysql",
    )
    created = await service.create(command)
    assert created.connection is None

    resource = await service.get_resource(
        agent_id=agent_id,
        resource_id=created.resource_id,
    )
    resource.status = DBInstanceStatus.READY
    await session.commit()

    ready = await service.describe(
        agent_id=agent_id,
        resource_id=created.resource_id,
    )
    assert ready.connection is not None
    assert ready.connection.host == "application.internal"
    assert ready.connection.port == 3306
    assert ready.connection.database
    assert ready.connection.username
    assert ready.connection.password
