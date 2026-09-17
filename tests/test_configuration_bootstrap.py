from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import (
    initialize_configuration,
    project_global_audit_documents,
    verify_bootstrap_token,
)
from server.configuration.repository import ConfigRepository
from server.configuration.types import ModuleDocument, ModuleState, SystemState
from server.core.config_crypto import ConfigCrypto, SecretEnvelope
from server.models import Base, ConfigBootstrapClaim, SystemConfig


ROOT_KEY = b"01234567890123456789012345678901"


@pytest.fixture
async def initialized():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    result = await initialize_configuration(repository, crypto)
    yield factory, repository, crypto, result
    await engine.dispose()


async def test_initialization_materializes_modules_and_encrypted_jwt(
    initialized,
) -> None:
    factory, _, crypto, result = initialized

    assert result.system_state == SystemState.SETUP
    assert result.bootstrap_token
    async with factory() as session:
        rows = (
            await session.execute(select(SystemConfig))
        ).scalars().all()
    keys = {row.config_key for row in rows}
    assert "setup.status" in keys
    assert "module.token_security" in keys
    assert "module.runtime_policy" in keys
    assert "module.agentic_db_purchase" not in keys
    assert "module.polarrag_tool_limits" in keys
    assert "module.enterprise_identity_sync" in keys
    token_row = next(row for row in rows if row.config_key == "module.token_security")
    document = ModuleDocument.model_validate_json(token_row.config_value)
    assert document.workflow_state == ModuleState.ACTIVE
    assert document.effective is not None
    config = document.effective.config
    assert "BEGIN PRIVATE KEY" not in token_row.config_value
    private_key = crypto.decrypt_field(
        SecretEnvelope.model_validate(config["private_key"]["$secret"]),
        module="token_security",
        field_path="private_key",
        schema_version=1,
    )
    assert "BEGIN PRIVATE KEY" in private_key
    assert config["active_kid"] in config["public_keys"]

    agent_token_row = next(
        row for row in rows if row.config_key == "module.agent_token_auth"
    )
    agent_token_document = ModuleDocument.model_validate_json(
        agent_token_row.config_value
    )
    assert agent_token_document.workflow_state == ModuleState.ACTIVE
    assert agent_token_document.effective is not None
    assert agent_token_document.effective.config == {"enabled": True}


async def test_fresh_bootstrap_uses_aliyun_schema_v2(initialized) -> None:
    _, repository, _, _ = initialized

    document = await repository.get_module("aliyun_access")
    setup_status = await repository.get_config_row("setup.status")

    assert document is not None
    assert setup_status is not None
    assert document.schema_version == 2
    assert json.loads(setup_status.config_value)["schema_version"] == 1


async def test_fresh_bootstrap_places_global_audit_in_observability(
    initialized,
) -> None:
    _, repository, _, _ = initialized

    sql = await repository.get_module("sql_security")
    observability = await repository.get_module("observability")

    assert sql is not None and sql.effective is not None
    assert observability is not None and observability.effective is not None
    assert sql.schema_version == 2
    assert observability.schema_version == 2
    assert "audit_enabled" not in sql.effective.config
    assert "audit_retention_days" not in sql.effective.config
    assert observability.effective.config["audit_enabled"] is True
    assert observability.effective.config["audit_retention_days"] == 180


async def _replace_module_document(
    factory,
    module: str,
    document: ModuleDocument,
) -> None:
    async with factory() as session:
        async with session.begin():
            row = await session.get(SystemConfig, f"module.{module}")
            assert row is not None
            row.config_value = document.model_dump_json(exclude_none=True)


async def _seed_legacy_global_audit(
    factory,
    repository: ConfigRepository,
    *,
    enabled: bool = False,
    retention_days: int = 37,
    with_drafts: bool = False,
) -> tuple[ModuleDocument, ModuleDocument]:
    sql = await repository.get_module("sql_security")
    observability = await repository.get_module("observability")
    assert sql is not None and sql.effective is not None
    assert observability is not None and observability.effective is not None
    sql_effective_config = {
        **sql.effective.config,
        "audit_enabled": enabled,
        "audit_retention_days": retention_days,
    }
    observability_effective_config = {
        key: value
        for key, value in observability.effective.config.items()
        if key not in {"audit_enabled", "audit_retention_days"}
    }
    sql_updates = {
        "schema_version": 1,
        "effective": sql.effective.model_copy(
            update={"config": sql_effective_config}
        ),
    }
    observability_updates = {
        "schema_version": 1,
        "effective": observability.effective.model_copy(
            update={"config": observability_effective_config}
        ),
    }
    if with_drafts:
        sql_updates.update(
            {
                "workflow_state": ModuleState.DRAFT,
                "draft": {
                    **sql_effective_config,
                    "requests_per_minute": 17,
                    "audit_enabled": True,
                    "audit_retention_days": 91,
                },
            }
        )
        observability_updates.update(
            {
                "workflow_state": ModuleState.DRAFT,
                "draft": {
                    **observability_effective_config,
                    "log_level": "debug",
                },
            }
        )
    legacy_sql = sql.model_copy(update=sql_updates)
    legacy_observability = observability.model_copy(
        update=observability_updates
    )
    await _replace_module_document(factory, "sql_security", legacy_sql)
    await _replace_module_document(
        factory,
        "observability",
        legacy_observability,
    )
    return legacy_sql, legacy_observability


async def test_repeated_initialization_preserves_legacy_global_audit(
    initialized,
) -> None:
    factory, repository, crypto, _ = initialized
    legacy_sql, legacy_observability = await _seed_legacy_global_audit(
        factory,
        repository,
    )
    before = await repository.global_version()

    result = await initialize_configuration(repository, crypto)

    sql = await repository.get_module("sql_security")
    observability = await repository.get_module("observability")
    assert result.bootstrap_token is None
    assert sql is not None and sql.effective is not None
    assert observability is not None and observability.effective is not None
    assert sql.schema_version == 1
    assert observability.schema_version == 1
    assert sql.revision == legacy_sql.revision
    assert observability.revision == legacy_observability.revision
    assert sql.effective.config["audit_enabled"] is False
    assert sql.effective.config["audit_retention_days"] == 37
    assert "audit_enabled" not in observability.effective.config
    assert "audit_retention_days" not in observability.effective.config
    assert await repository.global_version() == before

    await initialize_configuration(repository, crypto)
    assert await repository.global_version() == before


async def test_global_audit_projection_preserves_pending_module_drafts(
    initialized,
) -> None:
    factory, repository, crypto, _ = initialized
    await _seed_legacy_global_audit(
        factory,
        repository,
        with_drafts=True,
    )

    before = await repository.global_version()
    await initialize_configuration(repository, crypto)

    documents = project_global_audit_documents(
        await repository.list_modules()
    )
    sql = documents["sql_security"]
    observability = documents["observability"]
    assert sql is not None and sql.draft is not None
    assert observability is not None and observability.draft is not None
    assert sql.workflow_state == ModuleState.DRAFT
    assert observability.workflow_state == ModuleState.DRAFT
    assert sql.draft["requests_per_minute"] == 17
    assert "audit_enabled" not in sql.draft
    assert "audit_retention_days" not in sql.draft
    assert observability.draft["log_level"] == "debug"
    assert observability.draft["audit_enabled"] is True
    assert observability.draft["audit_retention_days"] == 91
    stored_sql = await repository.get_module("sql_security")
    stored_observability = await repository.get_module("observability")
    assert stored_sql is not None and stored_sql.schema_version == 1
    assert stored_observability is not None
    assert stored_observability.schema_version == 1
    assert await repository.global_version() == before


async def test_global_audit_migration_fails_closed_on_invalid_legacy_value(
    initialized,
) -> None:
    factory, repository, crypto, _ = initialized
    await _seed_legacy_global_audit(
        factory,
        repository,
        retention_days=0,
    )
    before = await repository.global_version()

    await initialize_configuration(repository, crypto)
    with pytest.raises(
        RuntimeError,
        match="legacy SQL audit configuration is invalid",
    ):
        project_global_audit_documents(await repository.list_modules())

    sql = await repository.get_module("sql_security")
    observability = await repository.get_module("observability")
    assert sql is not None and sql.schema_version == 1
    assert observability is not None and observability.schema_version == 1
    assert await repository.global_version() == before


async def test_concurrent_initialization_preserves_legacy_global_audit(
    tmp_path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'audit-migration.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    await initialize_configuration(repository, crypto)
    try:
        await _seed_legacy_global_audit(
            factory,
            repository,
            enabled=False,
            retention_days=45,
        )
        before = await repository.global_version()

        await asyncio.gather(
            initialize_configuration(repository, crypto),
            initialize_configuration(repository, crypto),
        )

        observability = await repository.get_module("observability")
        assert observability is not None
        assert observability.effective is not None
        assert observability.schema_version == 1
        assert "audit_enabled" not in observability.effective.config
        assert "audit_retention_days" not in observability.effective.config
        assert await repository.global_version() == before
    finally:
        await engine.dispose()


async def test_repeated_initialization_converges(initialized) -> None:
    factory, repository, crypto, first = initialized

    second, third = await asyncio.gather(
        initialize_configuration(repository, crypto),
        initialize_configuration(repository, crypto),
    )

    assert first.bootstrap_token
    assert second.bootstrap_token is None
    assert third.bootstrap_token is None
    async with factory() as session:
        claims = (await session.execute(select(ConfigBootstrapClaim))).scalars().all()
        token_rows = (
            (await session.execute(select(SystemConfig).where(SystemConfig.config_key == "module.token_security")))
            .scalars()
            .all()
        )
    assert len(claims) == 1
    assert len(token_rows) == 1


async def test_initialization_promotes_untouched_legacy_agent_token_draft(
    initialized,
) -> None:
    _, repository, crypto, _ = initialized
    current = await repository.get_module("agent_token_auth")
    assert current is not None
    legacy = ModuleDocument(
        schema_version=1,
        revision=0,
        workflow_state=ModuleState.DRAFT,
        initial_state=ModuleState.DRAFT,
        draft={"enabled": True},
    )
    row = await repository.get_config_row("module.agent_token_auth")
    assert row is not None
    async with repository.session_factory() as session:
        async with session.begin():
            stored = await session.get(
                SystemConfig, "module.agent_token_auth"
            )
            assert stored is not None
            stored.config_value = legacy.model_dump_json(exclude_none=True)

    result = await initialize_configuration(repository, crypto)
    promoted = await repository.get_module("agent_token_auth")

    assert result.bootstrap_token is None
    assert promoted is not None
    assert promoted.workflow_state == ModuleState.ACTIVE
    assert promoted.initial_state == ModuleState.ACTIVE
    assert promoted.effective is not None
    assert promoted.effective.config == {"enabled": True}


async def test_initialization_converges_edited_legacy_agent_token_draft(
    initialized,
) -> None:
    _, repository, crypto, _ = initialized
    edited = ModuleDocument(
        schema_version=1,
        revision=4,
        workflow_state=ModuleState.DRAFT,
        initial_state=ModuleState.DRAFT,
        draft={"enabled": False},
    )
    async with repository.session_factory() as session:
        async with session.begin():
            stored = await session.get(
                SystemConfig, "module.agent_token_auth"
            )
            assert stored is not None
            stored.config_value = edited.model_dump_json(exclude_none=True)

    await initialize_configuration(repository, crypto)

    converged = await repository.get_module("agent_token_auth")
    assert converged is not None
    assert converged.revision == 5
    assert converged.workflow_state == ModuleState.ACTIVE
    assert converged.initial_state == ModuleState.ACTIVE
    assert converged.draft is None
    assert converged.effective is not None
    assert converged.effective.revision == 5
    assert converged.effective.config == {"enabled": True}


async def test_repeated_initialization_restores_new_runtime_module(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'configuration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    await initialize_configuration(repository, crypto)
    try:
        async with factory() as session:
            row = await session.get(SystemConfig, "module.polarrag_tool_limits")
            assert row is not None
            await session.delete(row)
            await session.commit()
        before = await repository.global_version()

        second, third = await asyncio.gather(
            initialize_configuration(repository, crypto),
            initialize_configuration(repository, crypto),
        )

        restored = await repository.get_module("polarrag_tool_limits")
        assert second.bootstrap_token is None
        assert third.bootstrap_token is None
        assert restored is not None
        assert restored.workflow_state == ModuleState.ACTIVE
        assert restored.effective is not None
        assert restored.effective.config == {
            "enabled": True,
            "user_requests_per_minute": 60,
            "user_burst": 10,
            "agent_requests_per_minute": 120,
            "agent_burst": 20,
            "instance_max_inflight": 16,
            "max_fanout": 8,
            "max_exhaustive_knowledge_resources": 1000,
            "retry_after_seconds": 1,
            "upstream_request_timeout_ms": 20000,
        }
        assert await repository.global_version() == before + 1
    finally:
        await engine.dispose()


async def test_repeated_initialization_does_not_regenerate_token_security(
    initialized,
) -> None:
    factory, repository, crypto, _ = initialized
    async with factory() as session:
        row = await session.get(SystemConfig, "module.token_security")
        assert row is not None
        await session.delete(row)
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="token_security cannot be restored automatically",
    ):
        await initialize_configuration(repository, crypto)

    assert await repository.get_module("token_security") is None


async def test_repeated_initialization_does_not_restore_unlisted_module(
    initialized,
) -> None:
    factory, repository, crypto, _ = initialized
    async with factory() as session:
        row = await session.get(SystemConfig, "module.sql_security")
        assert row is not None
        await session.delete(row)
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="sql_security cannot be restored automatically",
    ):
        await initialize_configuration(repository, crypto)

    assert await repository.get_module("sql_security") is None


async def test_bootstrap_token_is_hashed_verified_and_consumed(
    initialized,
) -> None:
    factory, repository, _, result = initialized
    assert result.bootstrap_token is not None

    assert await verify_bootstrap_token(repository, result.bootstrap_token
    )
    await repository.consume_bootstrap_claim()
    assert not await verify_bootstrap_token(
        repository, result.bootstrap_token
    )

    async with factory() as session:
        claim = await session.get(ConfigBootstrapClaim, "bootstrap")
        assert claim is not None
        assert claim.token_hash != result.bootstrap_token
        assert claim.consumed_at is not None


async def test_invalid_token_increments_attempt_count(initialized) -> None:
    factory, repository, _, _ = initialized

    assert not await verify_bootstrap_token(repository, "wrong-token")

    async with factory() as session:
        claim = await session.get(ConfigBootstrapClaim, "bootstrap")
        assert claim is not None
        assert claim.failed_attempts == 1
