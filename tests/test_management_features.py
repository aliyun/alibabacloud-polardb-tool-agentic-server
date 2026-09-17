from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from server.features.knowledge import KnowledgeRuntime, KnowledgeState
from server.management.app import create_management_app
from server.management.identity import ManagedIdentityBinder
from server.management.types import ManagedTarget
from tests._configuration_helpers import create_config_context
from tests.test_management_app import _bearer_settings


@pytest.fixture
async def managed():
    context = await create_config_context()
    context.service.managed = True
    feature = KnowledgeRuntime(KnowledgeState(context.repository), managed=True)
    await feature.start()
    settings = _bearer_settings()
    await ManagedIdentityBinder(context.repository, settings.managed_identity).bind(
        ManagedTarget(instance_id="pmcp-test", generation=3)
    )
    runtime = SimpleNamespace(
        application_state=SimpleNamespace(config_service=context.service, knowledge_runtime=feature)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_management_app(runtime, settings)), base_url="http://test"
    ) as client:
        yield client, context, runtime
    await context.close()


async def test_managed_knowledge_requires_auth_identity_and_explicit_module(managed):
    client, _, _ = managed
    body = {"target": {"instance_id": "pmcp-test", "generation": 3}, "action": "status"}
    assert (await client.post("/api/internal/v1/features/knowledge", json=body)).status_code == 401
    client.headers["Authorization"] = "Bearer test-management-token"
    result = await client.post("/api/internal/v1/features/knowledge", json=body)
    assert result.status_code == 200
    assert result.json()["state"] == "DISABLED"
    assert result.json()["prepared"] is True
    assert "config" not in result.json()
    body["target"]["generation"] = 4
    assert (await client.post("/api/internal/v1/features/knowledge", json=body)).status_code == 409
    body["target"]["generation"] = 3
    body.update(action="config", parameters={"action": "describe", "module": "core_admin"})
    assert (await client.post("/api/internal/v1/features/knowledge", json=body)).status_code == 409
    body["parameters"]["module"] = "knowledge"
    assert (await client.post("/api/internal/v1/features/knowledge", json=body)).status_code == 200
    body["parameters"]["action"] = "reset"
    assert (await client.post("/api/internal/v1/features/knowledge", json=body)).status_code == 409


async def test_schema_inspection_is_authenticated_scoped_and_read_only(managed, monkeypatch):
    client, _, _ = managed
    inspect = AsyncMock(return_value={"compatible": True, "business_smoke": "PASSED"})
    monkeypatch.setattr("server.management.schema.inspect_runtime_schema", inspect)
    path = "/api/internal/v1/schema?instance_id=pmcp-test&generation=3"
    assert (await client.get(path)).status_code == 401
    inspect.assert_not_awaited()
    client.headers["Authorization"] = "Bearer test-management-token"
    assert (await client.get(path.replace("generation=3", "generation=4"))).status_code == 409
    inspect.assert_not_awaited()
    assert (await client.get(path)).json()["business_smoke"] == "PASSED"
    inspect.assert_awaited_once()
