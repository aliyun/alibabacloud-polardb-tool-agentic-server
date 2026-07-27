from __future__ import annotations

import base64
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.jwt_manager import reset_keys
from server.config import TenantProvisioningConfig, reset_config
from server.core.adapter_registry import AdapterRegistry
from server.core.agent_token_service import get_or_create_token
from server.core.crypto import encrypt
from server.core.db_instance_dispatcher import DBInstanceDispatcher
from server.core.db_instance_metrics import (
    DBInstanceMetricsMiddleware,
    DBInstanceMetricSample,
    set_db_instance_metric_sink,
)
from server.db import engine as engine_mod
from server.mcp.transport import mcp_lifespan, reset_mcp
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
    InstanceStatus,
    InstanceTopology,
    LeaseCleanupStep,
    LeaseProvisioningStep,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningCapacity,
    User,
)
from server.models.base import utc_now
from tests._helpers import init_test_jwt_keys

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class LifecycleAdapter:
    async def create(self, resource: DBInstanceResource) -> None:
        resource.provisioning_step = {
            LeaseProvisioningStep.PENDING:
                LeaseProvisioningStep.RESOURCE_CONFIG_CREATED,
            LeaseProvisioningStep.RESOURCE_CONFIG_CREATED:
                LeaseProvisioningStep.TENANT_CREATED,
            LeaseProvisioningStep.TENANT_CREATED:
                LeaseProvisioningStep.USER_CREATED,
            LeaseProvisioningStep.USER_CREATED:
                LeaseProvisioningStep.DATABASE_CREATED,
            LeaseProvisioningStep.DATABASE_CREATED:
                LeaseProvisioningStep.GRANTED,
        }[resource.provisioning_step]

    async def verify(self, resource: DBInstanceResource) -> None:
        resource.provisioning_step = LeaseProvisioningStep.VERIFIED

    async def delete(self, resource: DBInstanceResource) -> None:
        resource.cleanup_step = {
            LeaseCleanupStep.PENDING: LeaseCleanupStep.DATABASE_DROPPED,
            LeaseCleanupStep.DATABASE_DROPPED:
                LeaseCleanupStep.TENANT_DROPPED,
            LeaseCleanupStep.TENANT_DROPPED:
                LeaseCleanupStep.RESOURCE_CONFIG_DROPPED,
            LeaseCleanupStep.RESOURCE_CONFIG_DROPPED:
                LeaseCleanupStep.RESIDUE_VERIFIED,
        }[resource.cleanup_step]

    async def health_check(self, _backend: ProvisioningBackend) -> None:
        return None


def _jsonrpc(tool: str, arguments: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def _parse_sse(text: str) -> dict:
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    raise AssertionError("MCP response did not contain an SSE data event")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    reset_mcp()
    set_db_instance_metric_sink(None)
    yield
    set_db_instance_metric_sink(None)
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()


@pytest.fixture
async def e2e_env():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory

    async with factory() as session:
        admin = User(external_id="e2e-admin", display_name="E2E Admin")
        agent = Agent(name="e2e-agent", max_active_resources=10)
        instance = Instance(
            cluster_id="pc-e2e",
            name="E2E Cluster",
            topology=InstanceTopology.MULTITENANT,
            status=InstanceStatus.ACTIVE,
            host="pc.internal",
            port=3306,
        )
        session.add_all([admin, agent, instance])
        await session.flush()
        admin_credential = InstanceCredential(
            instance_id=instance.id,
            name="provisioning-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("admin"),
            password_ciphertext=encrypt("super-secret"),
            created_by_user_id=admin.id,
        )
        session.add(admin_credential)
        await session.flush()
        backend = ProvisioningBackend(
            instance_id=instance.id,
            admin_credential_id=admin_credential.id,
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
                    created_by_user_id=admin.id,
                ),
            ]
        )
        _, token = await get_or_create_token(session, agent.id, None)
        await session.commit()

    registry = AdapterRegistry()
    registry.register(
        instance.engine,
        instance.topology,
        LifecycleAdapter(),
    )
    dispatcher = DBInstanceDispatcher(
        factory,
        TenantProvisioningConfig(
            worker_claim_ttl_seconds=10,
            worker_claim_renew_seconds=1,
        ),
        registry,
        worker_id="e2e-worker",
    )
    app = create_app()
    async with mcp_lifespan():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield factory, dispatcher, agent.id, token, client
    await engine.dispose()


async def _call_tool(
    client: AsyncClient,
    token: str,
    tool: str,
    arguments: dict,
    request_id: int = 1,
) -> dict:
    response = await client.post(
        "/mcp",
        json=_jsonrpc(tool, arguments, request_id),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    server_timing = response.headers.get("server-timing", "")
    assert server_timing.startswith("agentic_db_tool;dur=")
    assert float(server_timing.rsplit("=", 1)[1]) >= 0
    event = _parse_sse(response.text)
    assert "error" not in event
    return event["result"]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


async def test_create_metric_covers_mcp_boundary_without_sensitive_labels(
    e2e_env,
):
    _factory, _dispatcher, _agent_id, token, client = e2e_env
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    result = await _call_tool(
        client,
        token,
        "create_db_instance",
        {
            "client_token": "metric-task",
            "db_type": "polardb_mysql",
        },
    )

    assert result["isError"] is False
    assert len(samples) == 1
    sample = samples[0]
    assert sample.name == "agentic_db_tool_duration_seconds"
    assert sample.labels == {
        "tool": "create_db_instance",
        "outcome": "ok",
        "backend_type": "multitenant",
    }
    assert sample.duration_seconds >= 0
    assert "metric-task" not in repr(sample)
    assert token not in repr(sample)


async def test_tool_metric_records_structured_error_outcome(e2e_env):
    _factory, _dispatcher, _agent_id, token, client = e2e_env
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    result = await _call_tool(
        client,
        token,
        "create_db_instance",
        {
            "client_token": "contains space",
            "db_type": "polardb_mysql",
        },
    )

    assert result["isError"] is True
    assert samples[0].labels["outcome"] == "error"


async def test_tool_metric_includes_authentication_failure(e2e_env):
    _factory, _dispatcher, _agent_id, _token, client = e2e_env
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    response = await client.post(
        "/mcp",
        json=_jsonrpc(
            "create_db_instance",
            {
                "client_token": "auth-failure",
                "db_type": "polardb_mysql",
            },
            request_id=9,
        ),
        headers={**MCP_HEADERS, "Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert len(samples) == 1
    assert samples[0].labels["outcome"] == "error"


async def test_metric_middleware_preserves_non_object_jsonrpc_handling(e2e_env):
    _factory, _dispatcher, _agent_id, token, client = e2e_env
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    response = await client.post(
        "/mcp",
        json=[],
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )

    assert response.status_code in {200, 400}
    assert samples == []


async def test_metric_middleware_preserves_invalid_tool_name_handling(e2e_env):
    _factory, _dispatcher, _agent_id, token, client = e2e_env
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": [], "arguments": {}},
        },
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )

    assert response.status_code in {200, 400}
    assert samples == []


async def test_metric_is_emitted_when_client_disconnects_during_response():
    samples: list[DBInstanceMetricSample] = []
    set_db_instance_metric_sink(samples.append)

    async def inner(_scope, receive, send):
        await receive()
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'data: {"result":{"isError":false}}\n\n',
                "more_body": False,
            }
        )

    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    _jsonrpc(
                        "create_db_instance",
                        {
                            "client_token": "disconnect",
                            "db_type": "polardb_mysql",
                        },
                    )
                ).encode(),
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def disconnected_send(_message):
        raise RuntimeError("client disconnected")

    middleware = DBInstanceMetricsMiddleware(inner)
    with pytest.raises(RuntimeError, match="client disconnected"):
        await middleware(
            {"type": "http", "method": "POST"},
            receive,
            disconnected_send,
        )

    assert len(samples) == 1
    assert samples[0].labels["outcome"] == "error"


async def test_full_agent_lifecycle_reaches_ready_then_deleted(e2e_env):
    factory, dispatcher, agent_id, token, client = e2e_env
    arguments = {
        "client_token": "lifecycle-task",
        "db_type": "polardb_mysql",
        "name": "Orders",
    }
    created = _payload(
        await _call_tool(client, token, "create_db_instance", arguments)
    )
    resource_id = created["db_instance_id"]
    assert created["status"] == "CREATING"
    assert await dispatcher.run_once() is True

    ready = _payload(
        await _call_tool(
            client,
            token,
            "describe_db_instance",
            {"db_instance_id": resource_id},
            request_id=2,
        )
    )
    assert ready["status"] == "READY"
    assert ready["host"] == "pc.internal"
    assert ready["port"] == 3306
    assert ready["password"]

    deleting = _payload(
        await _call_tool(
            client,
            token,
            "delete_db_instance",
            {"db_instance_id": resource_id},
            request_id=3,
        )
    )
    assert deleting["status"] == "DELETING"
    assert "password" not in deleting
    assert await dispatcher.run_once() is True

    deleted = _payload(
        await _call_tool(
            client,
            token,
            "describe_db_instance",
            {"db_instance_id": resource_id},
            request_id=4,
        )
    )
    assert deleted["status"] == "DELETED"
    assert "password" not in deleted

    replay = _payload(
        await _call_tool(
            client,
            token,
            "create_db_instance",
            arguments,
            request_id=5,
        )
    )
    assert replay["db_instance_id"] == resource_id
    assert replay["status"] == "DELETED"

    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource_id,
                    InstanceCredential.purpose
                    == CredentialPurpose.RESOURCE_ACCESS,
                )
            )
        ).scalar_one()
        capacity = {
            row.scope_type: row.active_count
            for row in (
                await session.execute(
                    select(ProvisioningCapacity).where(
                        ProvisioningCapacity.scope_id.in_(
                            [agent_id, resource.backend_id]
                        )
                    )
                )
            ).scalars()
        }
        assert resource.status == DBInstanceStatus.DELETED
        assert resource.capacity_released_at is not None
        assert credential.status == CredentialStatus.REVOKED
        assert credential.username_ciphertext is None
        assert credential.password_ciphertext is None
        assert capacity == {"agent": 0, "backend": 0}
