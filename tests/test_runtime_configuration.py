from __future__ import annotations

import pytest

from server.configuration.bootstrap import initialize_configuration
from server.configuration.module_migrations import ModuleMigrationError
from server.configuration.runtime import (
    ModuleLifecycleManager,
    RuntimeConfigStore,
    project_app_config,
)
from server.configuration.types import (
    ConfigActor,
    EffectiveConfig,
    ModuleState,
)
from tests._configuration_helpers import (
    create_config_context,
    seed_v1_global_audit,
    seed_active_assume_with_retained_direct,
    seed_v1_assume_document,
    seed_v1_direct_document,
)

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
    assert config.polarrag_tool_limits.enabled is True
    assert config.polarrag_tool_limits.user_requests_per_minute == 60
    assert config.polarrag_tool_limits.user_burst == 10
    assert config.polarrag_tool_limits.agent_requests_per_minute == 120
    assert config.polarrag_tool_limits.agent_burst == 20
    assert config.polarrag_tool_limits.instance_max_inflight == 16
    assert config.polarrag_tool_limits.max_fanout == 8
    assert config.polarrag_tool_limits.max_exhaustive_knowledge_resources == 1000
    assert config.polarrag_tool_limits.retry_after_seconds == 1
    assert config.polarrag_tool_limits.upstream_request_timeout_ms == 20_000
    assert config.enterprise_identity_sync.interval_seconds == 1800
    assert config.enterprise_identity_sync.initial_concurrency == 2
    assert config.enterprise_identity_sync.incremental_concurrency == 1
    assert config.audit.enabled is True
    assert config.audit.retention_days == 180
    assert store.poll_interval_seconds == 5


async def test_runtime_poll_projects_v1_global_audit_without_rewrite(
    context,
) -> None:
    await seed_v1_global_audit(
        context,
        enabled=False,
        retention_days=45,
    )
    before = await context.repository.global_version()
    await initialize_configuration(context.repository, context.crypto)
    store = RuntimeConfigStore(context.repository, context.crypto)

    result = await store.poll_once()
    config = store.current()

    sql = await context.repository.get_module("sql_security")
    observability = await context.repository.get_module("observability")
    assert result.reloaded is True
    assert store.last_error_code is None
    assert config.audit.enabled is False
    assert config.audit.retention_days == 45
    assert sql is not None and sql.schema_version == 1
    assert observability is not None and observability.schema_version == 1
    assert await context.repository.global_version() == before


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
    result = await store.poll_once()

    assert "agent_token_auth" in result.changed_modules
    assert store.module_active("agent_token_auth")


async def test_polarrag_limits_change_without_mutating_sql_security(
    context,
) -> None:
    store = RuntimeConfigStore(context.repository, context.crypto)
    await store.poll_once()
    document = await context.repository.get_module("polarrag_tool_limits")
    assert document is not None
    assert document.effective is not None
    document.effective.config = {
        "enabled": False,
        "user_requests_per_minute": 7,
        "user_burst": 3,
        "agent_requests_per_minute": 11,
        "agent_burst": 4,
        "instance_max_inflight": 2,
        "max_fanout": 1,
        "max_exhaustive_knowledge_resources": 1_000,
        "retry_after_seconds": 5,
        "upstream_request_timeout_ms": 12_000,
    }
    document.effective.revision += 1
    await context.repository.compare_and_set_module(
        "polarrag_tool_limits",
        expected_revision=document.revision,
        document=document,
    )

    result = await store.poll_once()
    config = store.current()

    assert result.changed_modules == ("polarrag_tool_limits",)
    assert config.polarrag_tool_limits.model_dump() == {
        "enabled": False,
        "user_requests_per_minute": 7,
        "user_burst": 3,
        "agent_requests_per_minute": 11,
        "agent_burst": 4,
        "instance_max_inflight": 2,
        "max_fanout": 1,
        "max_exhaustive_knowledge_resources": 1_000,
        "retry_after_seconds": 5,
        "upstream_request_timeout_ms": 12_000,
    }
    assert config.sql_security.rate_limit.requests_per_minute == 60
    assert config.sql_security.rate_limit.burst == 10


async def test_runtime_projects_v1_direct_document_without_rewrite(context) -> None:
    document = await seed_v1_direct_document(context)
    before = await context.repository.global_version()

    projected = project_app_config({"aliyun_access": document}, context.crypto
    )

    assert projected.aliyun.credential_mode == "direct_ak"
    assert projected.aliyun.access_key_id == "TEST1234567890ABCD"
    assert projected.aliyun.access_key_secret == "legacy-direct-secret"
    assert await context.repository.global_version() == before


async def test_runtime_decrypts_only_active_assume_block(context, monkeypatch) -> None:
    await seed_active_assume_with_retained_direct(context)
    document = await context.repository.get_module("aliyun_access")
    assert document is not None
    decrypt = context.crypto.decrypt_field
    paths: list[str] = []

    def record_paths(envelope, *, module, field_path, schema_version):
        paths.append(field_path)
        return decrypt(
            envelope,
            module=module,
            field_path=field_path,
            schema_version=schema_version,
        )

    monkeypatch.setattr(context.crypto, "decrypt_field", record_paths)

    projected = project_app_config(
        {"aliyun_access": document}, context.crypto
    )

    assert projected.aliyun.credential_mode == "assume_role"
    assert projected.aliyun.assume_role is not None
    assert projected.aliyun.direct_ak is None
    assert projected.aliyun.assume_role.source_access_key_id == "TEST0987654321WXYZ"
    assert projected.aliyun.credential_digest == context.crypto.digest(
        {
            "credential_mode": "assume_role",
            "assume_role": {
                "source_access_key_id": "TEST0987654321WXYZ",
                "source_access_key_secret": "source-secret",
                "role_arn": "acs:ram::123456789012:role/polardb",
                "role_session_name": "polardb-agentic",
            },
        }
    )
    assert projected.aliyun.config_revision == document.effective.revision
    assert paths == [
        "assume_role.source_access_key_id",
        "assume_role.source_access_key_secret",
    ]


async def test_runtime_projects_ecs_role_without_stored_access_keys(context) -> None:
    document = await context.repository.get_module("aliyun_access")
    assert document is not None
    document = document.model_copy(
        update={
            "effective": EffectiveConfig(
                revision=7,
                state=ModuleState.ACTIVE,
                config={
                    "credential_mode": "ecs_ram_role",
                    "region_id": "cn-hangzhou",
                    "openapi_network": "public",
                    "ecs_ram_role": {
                        "role_name": "pas-runtime",
                        "metadata_policy": "v2_only",
                    },
                },
            )
        }
    )

    projected = project_app_config({"aliyun_access": document}, context.crypto)

    assert projected.aliyun.ecs_ram_role is not None
    assert projected.aliyun.direct_ak is None
    assert projected.aliyun.assume_role is None
    assert projected.aliyun.has_active_credentials()
    assert projected.aliyun.config_revision == 7


async def test_runtime_projects_v1_assume_document_with_default_session_name(
    context,
) -> None:
    document = await seed_v1_assume_document(
        context, include_role_session_name=False
    )

    projected = project_app_config(
        {"aliyun_access": document}, context.crypto
    )

    assert projected.aliyun.credential_mode == "assume_role"
    assert projected.aliyun.access_key_id == "TEST0987654321WXYZ"
    assert projected.aliyun.access_key_secret == "legacy-source-secret"
    assert projected.aliyun.role_session_name == "polardb-agentic"


@pytest.mark.parametrize("schema_version", (0, 3))
async def test_runtime_rejects_unsupported_aliyun_schema(
    context, schema_version
) -> None:
    document = await seed_v1_direct_document(context)

    with pytest.raises(ModuleMigrationError, match="schema version"):
        project_app_config(
            {"aliyun_access": document.model_copy(update={"schema_version": schema_version})},
            context.crypto,
        )
