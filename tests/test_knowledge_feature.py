from __future__ import annotations

import sys
from contextlib import asynccontextmanager
import subprocess

import pytest
from sqlalchemy import delete

from server.configuration.bootstrap import initialize_configuration
from server.configuration.runtime import project_app_config
from server.models import SystemConfig
from tests._configuration_helpers import create_config_context


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


async def test_new_installation_disables_knowledge(context):
    documents = await context.repository.list_modules()
    assert documents["knowledge"].effective.config["enabled"] is False
    assert project_app_config(documents, context.crypto).knowledge.enabled is False


async def test_existing_installation_preserves_knowledge(context):
    async with context.repository.session_factory() as session:
        await session.execute(delete(SystemConfig).where(SystemConfig.config_key == "module.knowledge"))
        await session.commit()
    await initialize_configuration(context.repository, context.crypto)
    documents = await context.repository.list_modules()
    assert documents["knowledge"].effective.config["enabled"] is True
    assert project_app_config(documents, context.crypto).knowledge.enabled is True


def test_core_application_does_not_import_knowledge_api_or_tools():
    probe = """
import sys
from server.app import create_app
create_app()
for name in ('server.api.polarrag', 'server.api.platform_polarrag', 'server.api.polarrag_documents', 'server.mcp.tools.polarrag'):
    assert name not in sys.modules, name
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def set_enabled(context, enabled):
    from server.configuration.types import EffectiveConfig, ModuleState

    doc = await context.repository.get_module("knowledge")
    changed = doc.model_copy(
        update={
            "effective": EffectiveConfig(
                revision=doc.revision + 1, state=ModuleState.ACTIVE, config={"enabled": enabled}
            )
        }
    )
    await context.repository.compare_and_set_module("knowledge", expected_revision=doc.revision, document=changed)
    return changed.effective.revision


async def test_saved_configuration_does_not_hot_load_and_managed_activation_waits_for_all_replicas(context):
    from server.features.knowledge import KnowledgeRuntime, KnowledgeState
    from server.configuration.types import ConfigError

    state = KnowledgeState(context.repository)
    old = KnowledgeRuntime(state, managed=True, replica_id="old")
    await old.start()
    revision = await set_enabled(context, True)
    assert old.loaded_enabled is False
    async with _expect_unavailable(old):
        pass
    first = KnowledgeRuntime(state, managed=True, replica_id="new-1")
    second = KnowledgeRuntime(state, managed=True, replica_id="new-2")
    await first.start()
    await second.start()
    assert not await first.available()
    with pytest.raises(ConfigError, match="every live"):
        await state.confirm_activation(revision, ["new-1", "new-2"])
    await old.heartbeat(stopped=True)
    await state.confirm_activation(revision, ["new-1", "new-2"])
    assert await first.available() and await second.available()


@asynccontextmanager
async def _expect_unavailable(feature):
    from server.features.knowledge import KnowledgeUnavailable

    with pytest.raises(KnowledgeUnavailable):
        async with feature.operation("probe"):
            pytest.fail("closed capability admitted a request")
    yield


async def test_drain_rejects_new_work_and_waits_for_inflight(context):
    from server.features.knowledge import KnowledgeRuntime, KnowledgeState

    await set_enabled(context, True)
    state = KnowledgeState(context.repository)
    feature = KnowledgeRuntime(state, managed=False)
    await feature.start()
    async with feature.operation("upstream-query"):
        await state.begin_drain()
        assert (await state.disable_blockers())["in_flight"] == 1
        # Simulate an independent request, without inheriting operation context.
        import contextvars
        import asyncio

        task = asyncio.create_task(_assert_unavailable(feature), context=contextvars.Context())
        await task
    assert (await state.disable_blockers())["in_flight"] == 0


async def _assert_unavailable(feature):
    async with _expect_unavailable(feature):
        pass


async def test_enabling_requires_protected_read_test(context):
    from server.features.knowledge import activate_configuration
    from server.configuration.types import ConfigError, EffectiveConfig, ModuleState

    doc = await context.repository.get_module("knowledge")
    target = doc.model_copy(
        update={"effective": EffectiveConfig(revision=2, state=ModuleState.ACTIVE, config={"enabled": True})}
    )
    with pytest.raises(ConfigError, match="verify read access"):
        await activate_configuration(context.repository, expected_revision=doc.revision, document=target)
    assert (await context.repository.get_module("knowledge")).effective.config["enabled"] is False


async def test_disabled_http_paths_do_not_turn_into_spa_or_unknown_route():
    import httpx
    from fastapi import FastAPI
    from server.features.middleware import KnowledgeAdmissionMiddleware

    app = FastAPI()
    app.add_middleware(KnowledgeAdmissionMiddleware, runtime_provider=lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ("/api/polarrag/instances", "/api/v1/knowledge-bases", "/api/agents/a/polarrag-bindings"):
            response = await client.get(path)
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "KNOWLEDGE_NOT_ENABLED"
        assert (await client.get("/api/v1/external-auth/clients")).status_code == 404
        assert (await client.get("/api/agents/a/user-assignments")).status_code == 404


async def test_interrupted_operation_remains_a_disable_blocker(context):
    import asyncio
    from server.features.knowledge import KnowledgeRuntime, KnowledgeState

    await set_enabled(context, True)
    feature = KnowledgeRuntime(KnowledgeState(context.repository), managed=False)
    await feature.start()
    with pytest.raises(asyncio.CancelledError):
        async with feature.operation("external-write"):
            raise asyncio.CancelledError()
    assert (await feature.state.disable_blockers())["in_flight"] == 1


async def test_cancel_pending_activation_without_starting_knowledge(context):
    from server.features.knowledge import KnowledgeRuntime, KnowledgeState

    feature = KnowledgeRuntime(KnowledgeState(context.repository), managed=False)
    await feature.start()
    await set_enabled(context, True)
    assert (await feature.status())["state"] == "PENDING_RESTART"
    await feature.state.begin_drain()
    assert (await feature.status())["state"] == "DRAINING"
    assert feature.loaded_enabled is False
