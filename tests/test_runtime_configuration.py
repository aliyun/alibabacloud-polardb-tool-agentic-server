from __future__ import annotations

import pytest

from server.configuration.runtime import (
    ModuleLifecycleManager,
    RuntimeConfigStore,
)
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ModuleState,
)
from tests._configuration_helpers import create_config_context

ADMIN = ConfigActor(scope="admin:1", actor_type="admin")


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


async def test_initial_snapshot_projects_safe_defaults(context) -> None:
    store = RuntimeConfigStore(
        context.repository,
        context.crypto,
    )
    result = await store.poll_once()
    config = store.current()

    assert result.reloaded is True
    assert config.server.public_base_url == ""
    assert config.server.cors_origins == []
    assert config.server.log_level == "info"
    assert config.polardb.connection_pool.max_connections_per_pool == 5
    assert store.poll_interval_seconds == 5


async def test_unchanged_version_does_not_reload(context) -> None:
    store = RuntimeConfigStore(
        context.repository,
        context.crypto,
    )
    await store.poll_once()
    previous = store.current()

    result = await store.poll_once()

    assert result.reloaded is False
    assert store.current() is previous


async def test_changed_module_is_atomically_swapped(context) -> None:
    store = RuntimeConfigStore(
        context.repository,
        context.crypto,
    )
    await store.poll_once()
    old = store.current()
    runtime = await context.repository.get_module("runtime_policy")
    runtime.draft = {
        **runtime.effective.config,
        "config_poll_interval_seconds": 2,
        "max_connections_per_pool": 9,
    }
    runtime.workflow_state = ModuleState.VALIDATED
    runtime.last_validation = None
    runtime.effective.config = runtime.draft
    runtime.effective.revision += 1
    await context.repository.compare_and_set_module(
        "runtime_policy",
        expected_revision=runtime.revision,
        document=runtime,
    )

    result = await store.poll_once()
    new = store.current()

    assert result.changed_modules == ("runtime_policy",)
    assert old.polardb.connection_pool.max_connections_per_pool == 5
    assert new.polardb.connection_pool.max_connections_per_pool == 9
    assert store.poll_interval_seconds == 2


async def test_required_adapter_failure_retains_previous_snapshot(
    context,
) -> None:
    calls: list[str] = []

    async def fail(_old, new):
        if new.polardb.connection_pool.max_total_pools == 999:
            calls.append("apply")
            raise RuntimeError("sanitized failure")

    manager = ModuleLifecycleManager({"runtime_policy": fail})
    store = RuntimeConfigStore(
        context.repository,
        context.crypto,
        lifecycle_manager=manager,
    )
    await store.poll_once()
    old = store.current()
    runtime = await context.repository.get_module("runtime_policy")
    runtime.effective.config["max_total_pools"] = 999
    runtime.effective.revision += 1
    await context.repository.compare_and_set_module(
        "runtime_policy",
        expected_revision=runtime.revision,
        document=runtime,
    )

    result = await store.poll_once()

    assert result.reloaded is False
    assert result.error_code == "RUNTIME_APPLY_FAILED"
    assert store.current() is old
    assert calls == ["apply"]
    assert store.last_error_code == "RUNTIME_APPLY_FAILED"


async def test_activation_is_visible_after_poll(context) -> None:
    store = RuntimeConfigStore(context.repository, context.crypto)
    await store.poll_once()
    saved = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="agent_token_auth",
            expected_revision=0,
            config={"enabled": True},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    await context.service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="agent_token_auth",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="runtime-agent-auth",
        ),
        ADMIN,
    )

    result = await store.poll_once()

    assert result.changed_modules == ("agent_token_auth",)
    assert store.module_active("agent_token_auth")
