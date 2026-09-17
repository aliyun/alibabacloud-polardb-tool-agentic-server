"""Browser configuration must not imply authority to admit a managed fleet."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import base64

import httpx
import pytest
from fastapi import FastAPI

from server.api import configuration, features
from server.auth.jwt_manager import create_access_token, reset_keys
from server.configuration.types import ConfigActor, ConfigCommand, ConfigError, SystemState
from server.db.engine import get_session
from server.features.knowledge import KnowledgeRuntime, KnowledgeState
from server.models import AuthProvider, User, UserRole, UserStatus
from tests._configuration_helpers import create_config_context, ROOT_KEY
from tests._helpers import init_test_jwt_keys


@pytest.fixture
async def console(monkeypatch):
    reset_keys()
    init_test_jwt_keys()
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(ROOT_KEY).decode())
    context = await create_config_context()
    context.service.managed = True
    monkeypatch.setattr(context.service, "_system_state", AsyncMock(return_value=SystemState.READY))
    feature = KnowledgeRuntime(KnowledgeState(context.repository), managed=True, replica_id="old")
    await feature.start()
    async with context.repository.session_factory() as session:
        admin = User(external_id="admin", display_name="Admin", auth_provider=AuthProvider.BUILTIN,
                     role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        user = User(external_id="user", display_name="User", auth_provider=AuthProvider.BUILTIN,
                    role=UserRole.MEMBER, status=UserStatus.ACTIVE)
        session.add_all([admin, user])
        await session.commit()
    app = FastAPI()
    app.state.config_service = context.service
    app.state.knowledge_runtime = feature
    app.include_router(features.router, prefix="/api")
    app.include_router(configuration.router, prefix="/api")
    async def sessions():
        async with context.repository.session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        client.headers["Authorization"] = "Bearer " + create_access_token({"sub": admin.id, "credential_epoch": 1})
        yield SimpleNamespace(client=client, context=context, feature=feature, admin=admin, user=user)
    await context.close()
    reset_keys()


async def test_managed_admin_can_save_connection_but_not_confirm_fleet(console):
    response = await console.client.post("/api/features/knowledge/connections", json={
        "name": "RAG", "scheme": "https", "host": "rag.example.com", "port": 9200,
        "username": "test", "password": "test-password", "tls_verify": True,
    })
    assert response.status_code == 201, response.text
    assert "password" not in response.text
    assert (await console.client.get("/api/features/knowledge/options")).json()["connections"][0]["name"] == "RAG"
    confirm = await console.client.post("/api/features/knowledge/confirm-activation", json={
        "expected_revision": 1, "replica_ids": ["old"],
    })
    assert confirm.status_code == 409
    assert confirm.json()["detail"]["code"] == "KNOWLEDGE_MANAGED_BY_CONTROL_PLANE"


async def test_managed_admin_saves_verified_desired_revision_without_admission(console, monkeypatch):
    check = AsyncMock()
    monkeypatch.setattr("server.features.knowledge.validate_read_access", check)
    client = console.client
    doc = await console.context.repository.get_module("knowledge")
    saved = await client.post("/api/config", json={"action": "save_draft", "module": "knowledge",
        "expected_revision": doc.revision, "config": {"enabled": True,
        "validation_resource_id": "resource", "validation_user_id": console.admin.id}})
    assert saved.status_code == 200, saved.text
    checked = await client.post("/api/config", json={"action": "validate", "module": "knowledge",
        "expected_revision": saved.json()["module"]["revision"]})
    assert checked.status_code == 200, checked.text
    activated = await client.post("/api/config", json={"action": "activate", "module": "knowledge",
        "expected_revision": checked.json()["module"]["revision"],
        "validation_id": checked.json()["validation"]["validation_id"], "idempotency_key": "managed-enable"})
    assert activated.status_code == 200, activated.text
    check.assert_awaited()
    status = (await client.get("/api/features/knowledge")).json()
    assert status["state"] == "PENDING_RESTART"
    assert status["desired_enabled"] and not status["available"]
    assert console.feature.loaded_enabled is False
    # Even fully prepared managed replicas cannot self-admit.
    await console.feature.heartbeat(stopped=True)
    new = KnowledgeRuntime(console.feature.state, managed=True, replica_id="new")
    await new.start()
    assert (await new.status())["state"] == "ACTIVATING"
    assert not await new.available()


async def test_configuration_does_not_skip_protected_read_validation(console):
    doc = await console.context.repository.get_module("knowledge")
    saved = await console.client.post("/api/config", json={"action": "save_draft", "module": "knowledge",
        "expected_revision": doc.revision, "config": {"enabled": True}})
    assert saved.status_code == 200, saved.text
    checked = await console.client.post("/api/config", json={"action": "validate", "module": "knowledge",
        "expected_revision": saved.json()["module"]["revision"]})
    assert checked.status_code == 200
    activated = await console.client.post("/api/config", json={"action": "activate", "module": "knowledge",
        "expected_revision": checked.json()["module"]["revision"],
        "validation_id": checked.json()["validation"]["validation_id"], "idempotency_key": "unverified"})
    assert activated.status_code == 409
    assert activated.json()["detail"]["code"] == "KNOWLEDGE_READ_TEST_REQUIRED"
    assert not await console.feature.available()


async def test_managed_console_mutations_still_require_admin_and_csrf(console):
    client = console.client
    client.headers["Authorization"] = "Bearer " + create_access_token({"sub": console.user.id, "credential_epoch": 1})
    assert (await client.post("/api/features/knowledge/drain")).status_code == 403
    assert (await client.post("/api/config", json={"action": "describe", "module": "knowledge"})).status_code == 403
    del client.headers["Authorization"]
    assert (await client.post("/api/features/knowledge/drain")).status_code == 403
    client.cookies.set("session_token", create_access_token({"sub": console.admin.id, "credential_epoch": 1}))
    assert (await client.post("/api/features/knowledge/drain")).status_code == 403
    client.headers["X-PAS-CSRF"] = "1"
    assert (await client.post("/api/features/knowledge/drain")).status_code == 200


async def test_managed_initializer_cannot_configure_knowledge(console):
    with pytest.raises(ConfigError) as error:
        await console.context.service.execute(ConfigCommand(action="save_draft", module="knowledge",
            expected_revision=1, config={"enabled": False}),
            ConfigActor(scope="init", actor_type="managed_initializer"))
    assert error.value.code == "CONFIG_OPERATION_NOT_ALLOWED"


@pytest.mark.parametrize("action", ["reset", "skip", "disable"])
async def test_managed_configuration_cannot_bypass_feature_workflow(console, action):
    doc = await console.context.repository.get_module("knowledge")
    response = await console.client.post("/api/config", json={"action": action, "module": "knowledge",
        "expected_revision": doc.revision, "idempotency_key": "unsafe-" + action})
    assert response.status_code == 409
    assert (await console.context.repository.get_module("knowledge")).revision == doc.revision
