from __future__ import annotations

import base64
import os

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import reset_config
from server.core.crypto import encrypt
from server.db.engine import get_session
from server.mcp.agent_openapi import router as agent_openapi_router
from server.mcp.db_instance_rest import router as agent_db_instance_router
from server.models import (
    Agent,
    AgentProvisioningBinding,
    Base,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    User,
)
from server.models.base import utc_now
from tests._helpers import create_test_agent_token


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
async def rest_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with sessions() as session:
        creator = User(external_id="rest-admin", display_name="REST Admin")
        agent = Agent(name="rest-agent", max_active_resources=10)
        other_agent = Agent(name="other-rest-agent", max_active_resources=10)
        instance = Instance(
            cluster_id="rest-cluster",
            name="REST backend",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            status=InstanceStatus.ACTIVE,
            host="rest.internal",
            port=3306,
        )
        session.add_all([creator, agent, other_agent, instance])
        await session.flush()
        _, token = await create_test_agent_token(session, agent.id)
        _, other_token = await create_test_agent_token(session, other_agent.id)
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
                AgentProvisioningBinding(
                    agent_id=other_agent.id,
                    backend_id=backend.id,
                    created_by_user_id=creator.id,
                ),
            ]
        )
        await session.commit()
        agent_id = agent.id

    app = FastAPI()
    app.include_router(agent_db_instance_router)
    app.include_router(agent_openapi_router)

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client, sessions, agent_id, token, other_token
    await engine.dispose()


async def test_create_poll_ready_and_delete_contract(rest_context):
    client, sessions, agent_id, token, _ = rest_context
    headers = {"Authorization": f"Bearer {token}"}
    create_response = await client.post(
        "/mcp/rest/db-instances",
        headers=headers,
        json={
            "client_token": "rest-create-1",
            "name": "sandbox-db",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        },
    )
    assert create_response.status_code == 202
    assert create_response.headers["cache-control"] == "no-store"
    assert create_response.headers["retry-after"] == "5"
    created = create_response.json()
    assert created["status"] == "CREATING"
    assert created["retry_after_seconds"] == 5
    resource_id = created["resource_id"]
    assert create_response.headers["location"].endswith(resource_id)

    replay = await client.post(
        "/mcp/rest/db-instances",
        headers=headers,
        json={
            "client_token": "rest-create-1",
            "name": "sandbox-db",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        },
    )
    assert replay.status_code == 202
    assert replay.json()["resource_id"] == resource_id

    async with sessions() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource is not None
        assert resource.owner_agent_id == agent_id
        resource.status = DBInstanceStatus.READY
        await session.commit()

    ready = await client.get(
        f"/mcp/rest/db-instances/{resource_id}", headers=headers
    )
    assert ready.status_code == 200
    assert ready.headers["cache-control"] == "no-store"
    assert ready.json()["connection"] == {
        "host": "rest.internal",
        "port": 3306,
        "database": ready.json()["connection"]["database"],
        "username": ready.json()["connection"]["username"],
        "password": ready.json()["connection"]["password"],
    }

    deleted = await client.delete(
        f"/mcp/rest/db-instances/{resource_id}", headers=headers
    )
    assert deleted.status_code == 202
    assert deleted.headers["cache-control"] == "no-store"
    assert deleted.json()["status"] == "DELETING"
    assert deleted.json()["connection"] is None


async def test_owner_hiding_and_stable_idempotency_error(rest_context):
    client, _, _, token, other_token = rest_context
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/mcp/rest/db-instances",
        headers=headers,
        json={
            "client_token": "rest-conflict",
            "name": "first",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        },
    )
    resource_id = response.json()["resource_id"]

    hidden = await client.get(
        f"/mcp/rest/db-instances/{resource_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "RESOURCE_NOT_FOUND"

    conflict = await client.post(
        "/mcp/rest/db-instances",
        headers=headers,
        json={
            "client_token": "rest-conflict",
            "name": "changed",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert conflict.json()["request_id"]


async def test_deleted_resource_returns_204(rest_context):
    client, sessions, _, token, _ = rest_context
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/mcp/rest/db-instances",
        headers=headers,
        json={
            "client_token": "rest-deleted",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        },
    )
    resource_id = response.json()["resource_id"]
    async with sessions() as session:
        resource = await session.scalar(
            select(DBInstanceResource).where(DBInstanceResource.id == resource_id)
        )
        assert resource is not None
        resource.status = DBInstanceStatus.DELETED
        await session.commit()

    deleted = await client.delete(
        f"/mcp/rest/db-instances/{resource_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert deleted.content == b""


async def test_internal_value_error_message_is_redacted(
    rest_context,
    monkeypatch,
):
    client, _, _, token, _ = rest_context

    async def fail_create(self, command):
        raise ValueError("PAS_ENCRYPTION_KEY is required")

    monkeypatch.setattr(
        "server.mcp.db_instance_rest.DBInstanceApplicationService.create",
        fail_create,
    )
    response = await client.post(
        "/mcp/rest/db-instances",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "client_token": "redacted-error",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ARGUMENT"
    assert response.json()["message"] == "The database request is invalid."
    assert "PAS_ENCRYPTION_KEY" not in response.text
