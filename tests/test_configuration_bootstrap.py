from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import (
    initialize_configuration,
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
    token_row = next(
        row for row in rows if row.config_key == "module.token_security"
    )
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
        claims = (
            await session.execute(select(ConfigBootstrapClaim))
        ).scalars().all()
        token_rows = (
            await session.execute(
                select(SystemConfig).where(
                    SystemConfig.config_key == "module.token_security"
                )
            )
        ).scalars().all()
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


async def test_bootstrap_token_is_hashed_verified_and_consumed(
    initialized,
) -> None:
    factory, repository, _, result = initialized
    assert result.bootstrap_token is not None

    assert await verify_bootstrap_token(
        repository, result.bootstrap_token
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
