from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.core.db_instance_service import (
    DBInstanceNotFound,
    NoProvisioningBackend,
    create_db_instance_resource,
    delete_db_instance_resource,
    describe_db_instance_resource,
)
from server.auth.principal import Principal, PrincipalKind
from server.config import reset_config
from server.models import (
    Agent,
    AgentProvisioningBinding,
    Base,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningCapacity,
    User,
)
from server.models.base import utc_now


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


@pytest.fixture(autouse=True)
def encryption_config(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_config()
    yield
    reset_config()


async def _seed_backend(session: AsyncSession) -> tuple[Agent, ProvisioningBackend]:
    creator = User(external_id="admin", display_name="Admin")
    agent = Agent(name="service-agent", max_active_resources=2)
    instance = Instance(
        cluster_id="cluster-service",
        name="Service Backend",
        engine=InstanceEngine.POLARDB_MYSQL,
        topology=InstanceTopology.MULTITENANT,
        status=InstanceStatus.ACTIVE,
        host="service.internal",
        port=3306,
    )
    session.add_all([creator, agent, instance])
    await session.flush()
    credential = InstanceCredential(
        instance_id=instance.id,
        name="provisioning-admin",
        purpose=CredentialPurpose.PROVISIONING_ADMIN,
        capability=CredentialCapability.ADMIN,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
        created_by_user_id=creator.id,
    )
    session.add(credential)
    await session.flush()
    backend = ProvisioningBackend(
        instance_id=instance.id,
        admin_credential_id=credential.id,
        priority=1,
        max_active_resources=2,
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
    await session.commit()
    return agent, backend


async def test_create_resource_reserves_backend_and_agent_capacity(session):
    agent, backend = await _seed_backend(session)
    agent_id = agent.id
    backend_id = backend.id

    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="orders",
        db_type="polardb_mysql",
    )

    assert resource.backend_id == backend_id
    assert resource.owner_agent_id == agent_id
    capacities = (
        await session.execute(
            select(ProvisioningCapacity).order_by(
                ProvisioningCapacity.scope_type
            )
        )
    ).scalars().all()
    assert [(row.scope_type, row.active_count) for row in capacities] == [
        ("agent", 1),
        ("backend", 1),
    ]
    assert (
        await session.execute(select(DBInstanceResource))
    ).scalar_one().id == resource.id


async def test_create_without_an_active_provisioning_binding_fails_closed(session):
    agent = Agent(name="unbound-agent", max_active_resources=2)
    session.add(agent)
    await session.commit()

    with pytest.raises(NoProvisioningBackend):
        await create_db_instance_resource(
            session,
            agent_id=agent.id,
            client_token="deploy-1",
            name=None,
            db_type="polardb_mysql",
        )


async def test_owner_can_describe_ready_resource_with_credentials(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="describe-ready",
        name="orders",
        db_type="polardb_mysql",
    )
    resource.status = DBInstanceStatus.READY
    await session.commit()

    result = await describe_db_instance_resource(
        session,
        Principal(PrincipalKind.AGENT, agent_id),
        resource.id,
    )

    assert result["db_instance_id"] == resource.id
    assert result["source"] == "provisioned"
    assert result["status"] == "READY"
    assert result["provisioning_step"] == "PENDING"
    assert result["cleanup_step"] == "PENDING"
    assert result["created_at"]
    assert result["updated_at"]
    assert result["host"] == "service.internal"
    assert result["username"].startswith("agentic@t")
    assert result["password"]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("status", CredentialStatus.REVOKED),
        ("purpose", CredentialPurpose.DIRECT_ACCESS),
        ("capability", CredentialCapability.READONLY),
        ("capability", CredentialCapability.ADMIN),
        ("version", 2),
        ("resource_id", "dbi-wrong-owner"),
        ("instance_id", "instance-wrong-owner"),
        ("database_name", "wrong-database"),
        ("username_ciphertext", None),
        ("password_ciphertext", None),
    ],
)
async def test_describe_omits_invalid_resource_credential_before_decrypt(
    session,
    monkeypatch,
    field,
    invalid_value,
):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token=f"invalid-credential-{field}-{invalid_value}",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = DBInstanceStatus.READY
    await session.commit()
    await session.refresh(resource, ["credentials"])
    credential = resource.credentials[0]
    setattr(credential, field, invalid_value)

    def reject_decrypt(_ciphertext):
        raise AssertionError("invalid resource credential must not be decrypted")

    monkeypatch.setattr(
        "server.core.db_instance_contract.decrypt",
        reject_decrypt,
    )
    with session.no_autoflush:
        result = await describe_db_instance_resource(
            session,
            Principal(PrincipalKind.AGENT, agent_id),
            resource.id,
        )

    assert result["db_instance_id"] == resource.id
    assert result["status"] == "READY"
    assert result["capabilities"] == ["list", "describe", "delete"]
    assert not {"host", "port", "database", "username", "password"} & set(
        result
    )


async def test_describe_omits_ambiguous_resource_credentials(
    session,
    monkeypatch,
):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="ambiguous-credentials",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = DBInstanceStatus.READY
    await session.commit()
    await session.refresh(resource, ["credentials"])
    original = resource.credentials[0]
    resource.credentials.append(
        InstanceCredential(
            resource_id=resource.id,
            name="duplicate-resource-access",
            purpose=CredentialPurpose.RESOURCE_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=original.username_ciphertext,
            password_ciphertext=original.password_ciphertext,
            database_name=resource.database_name,
            version=1,
        )
    )

    def reject_decrypt(_ciphertext):
        raise AssertionError("ambiguous resource credentials must not be decrypted")

    monkeypatch.setattr(
        "server.core.db_instance_contract.decrypt",
        reject_decrypt,
    )
    with session.no_autoflush:
        result = await describe_db_instance_resource(
            session,
            Principal(PrincipalKind.AGENT, agent_id),
            resource.id,
        )

    assert result["db_instance_id"] == resource.id
    assert result["capabilities"] == ["list", "describe", "delete"]
    assert not {"host", "port", "database", "username", "password"} & set(
        result
    )


async def test_describe_omits_missing_resource_credential(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="missing-resource-credential",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = DBInstanceStatus.READY
    await session.commit()
    await session.refresh(resource, ["credentials"])
    resource.credentials.clear()

    with session.no_autoflush:
        result = await describe_db_instance_resource(
            session,
            Principal(PrincipalKind.AGENT, agent_id),
            resource.id,
        )

    assert result["db_instance_id"] == resource.id
    assert result["capabilities"] == ["list", "describe", "delete"]
    assert not {"host", "port", "database", "username", "password"} & set(
        result
    )


@pytest.mark.parametrize(
    "ciphertext",
    (
        "not-base64",
        base64.b64encode(b"not-a-fernet-token").decode("ascii"),
    ),
)
async def test_describe_omits_corrupt_resource_credential(
    session, ciphertext
):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token=f"corrupt-resource-credential-{len(ciphertext)}",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = DBInstanceStatus.READY
    await session.commit()
    await session.refresh(resource, ["credentials"])
    resource.credentials[0].password_ciphertext = ciphertext

    with session.no_autoflush:
        result = await describe_db_instance_resource(
            session,
            Principal(PrincipalKind.AGENT, agent_id),
            resource.id,
        )

    assert result["db_instance_id"] == resource.id
    assert result["capabilities"] == ["list", "describe", "delete"]
    assert not {"host", "port", "database", "username", "password"} & set(
        result
    )


@pytest.mark.parametrize("principal_kind", [PrincipalKind.AGENT, PrincipalKind.USER])
async def test_describe_does_not_reveal_resource_across_principals(
    session,
    principal_kind,
):
    agent, _ = await _seed_backend(session)
    resource = await create_db_instance_resource(
        session,
        agent_id=agent.id,
        client_token="private-resource",
        name=None,
        db_type="polardb_mysql",
    )

    with pytest.raises(DBInstanceNotFound):
        await describe_db_instance_resource(
            session,
            Principal(principal_kind, "different-principal"),
            resource.id,
        )


async def test_delete_only_transitions_the_owning_agents_resource(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="delete-resource",
        name=None,
        db_type="polardb_mysql",
    )
    resource_id = resource.id

    with pytest.raises(DBInstanceNotFound):
        await delete_db_instance_resource(
            session,
            "different-agent",
            resource_id,
        )

    deleted = await delete_db_instance_resource(
        session,
        agent_id,
        resource_id,
    )
    assert deleted.status == DBInstanceStatus.DELETING


async def test_delete_requeues_delete_failed_and_clears_retry_state(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="retry-delete",
        name=None,
        db_type="polardb_mysql",
    )
    resource.status = DBInstanceStatus.DELETE_FAILED
    resource.retry_count = 9
    resource.next_retry_at = utc_now()
    resource.failure_reason = "Cleanup step failed with RuntimeError"
    resource.worker_id = "old-worker"
    resource.worker_lease_until = utc_now()
    resource_id = resource.id
    await session.commit()

    result = await delete_db_instance_resource(session, agent_id, resource_id)

    assert result.status == DBInstanceStatus.DELETING
    assert result.retry_count == 0
    assert result.next_retry_at is None
    assert result.failure_reason is None
    assert result.worker_id is None
    assert result.worker_lease_until is None
