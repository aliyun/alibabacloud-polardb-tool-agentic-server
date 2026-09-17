from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.configuration.service import ConfigService
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)
from server.core.config_crypto import ConfigCrypto
from server.models import Base, PasswordState, User


ROOT_KEY = b"01234567890123456789012345678901"


@pytest.fixture
async def managed_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    initialization = await initialize_configuration(
        repository,
        crypto,
        managed=True,
    )
    await repository.bind_managed_identity(
        instance_id="pmcp-a",
        generation=1,
    )
    yield factory, repository, ConfigService(repository, crypto), initialization
    await engine.dispose()


async def test_managed_bootstrap_never_returns_token(managed_context) -> None:
    _, repository, _, initialization = managed_context

    assert initialization.bootstrap_token is None
    assert await repository.get_bootstrap_claim() is not None


async def test_managed_initializer_allowlist_rejects_password(
    managed_context,
) -> None:
    _, _, service, _ = managed_context
    actor = ConfigActor(
        scope="managed:pmcp-a:1",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )

    with pytest.raises(ConfigError) as captured:
        await service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="core_admin",
                expected_revision=0,
                config={"password": "must-not-be-accepted"},
            ),
            actor,
        )

    assert captured.value.code == "CONFIG_OPERATION_NOT_ALLOWED"


async def test_managed_save_draft_requires_key_and_replays_exactly(
    managed_context,
) -> None:
    _, _, service, _ = managed_context
    actor = ConfigActor(
        scope="managed:pmcp-a:1",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )
    without_key = ConfigCommand(
        action=ConfigAction.SAVE_DRAFT,
        module="core_admin",
        expected_revision=0,
        config={"username": "admin"},
    )
    with pytest.raises(ConfigError) as captured:
        await service.execute(without_key, actor)
    assert captured.value.code == "IDEMPOTENCY_KEY_REQUIRED"

    with pytest.raises(ConfigError) as activation_error:
        await service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="core_admin",
                expected_revision=0,
            ),
            actor,
        )
    assert activation_error.value.code == "IDEMPOTENCY_KEY_REQUIRED"

    command = without_key.model_copy(update={"idempotency_key": "save-admin"})
    saved = await service.execute(command, actor)
    replayed = await service.execute(command, actor)

    assert replayed == saved
    assert saved.module["revision"] == 1


async def test_managed_validate_replays_same_proof_without_new_revision(
    managed_context,
) -> None:
    _, _, service, _ = managed_context
    actor = ConfigActor(
        scope="managed:pmcp-a:1",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )
    saved = await service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="core_admin",
            expected_revision=0,
            idempotency_key="save-for-validation",
            config={"username": "admin"},
        ),
        actor,
    )
    command = ConfigCommand(
        action=ConfigAction.VALIDATE,
        module="core_admin",
        expected_revision=saved.module["revision"],
        idempotency_key="validate-admin",
    )

    validated = await service.execute(command, actor)
    replayed = await service.execute(command, actor)

    assert replayed == validated
    assert validated.module["revision"] == 3
    assert (
        replayed.validation["validation_id"] == (validated.validation["validation_id"])
    )


async def test_managed_writes_prepare_once_and_skip_replay(
    managed_context,
    monkeypatch,
) -> None:
    _, _, service, _ = managed_context
    actor = ConfigActor(
        scope="managed:pmcp-a:1",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )
    prepare = AsyncMock(wraps=service._prepare_document_for_write)
    monkeypatch.setattr(service, "_prepare_document_for_write", prepare)

    save_command = ConfigCommand(
        action=ConfigAction.SAVE_DRAFT,
        module="core_admin",
        expected_revision=0,
        idempotency_key="prepared-save-admin",
        config={"username": "admin"},
    )
    saved = await service.execute(save_command, actor)
    assert await service.execute(save_command, actor) == saved

    validate_command = ConfigCommand(
        action=ConfigAction.VALIDATE,
        module="core_admin",
        expected_revision=saved.module["revision"],
        idempotency_key="prepared-validate-admin",
    )
    validated = await service.execute(validate_command, actor)
    assert await service.execute(validate_command, actor) == validated

    activate_command = ConfigCommand(
        action=ConfigAction.ACTIVATE,
        module="core_admin",
        expected_revision=validated.module["revision"],
        validation_id=validated.validation["validation_id"],
        idempotency_key="prepared-activate-admin",
    )
    activated = await service.execute(activate_command, actor)
    assert await service.execute(activate_command, actor) == activated

    assert prepare.await_args_list == [
        call("core_admin", 0),
        call("core_admin", 1),
        call("core_admin", 3),
    ]


async def test_managed_activation_creates_reset_required_admin_atomically(
    managed_context,
) -> None:
    from server.auth.builtin import authenticate_builtin

    factory, repository, service, _ = managed_context
    actor = ConfigActor(
        scope="managed:pmcp-a:1",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )
    saved = await service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="core_admin",
            expected_revision=0,
            idempotency_key="activation-save-admin",
            config={"username": "admin"},
        ),
        actor,
    )
    validated = await service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="core_admin",
            expected_revision=saved.module["revision"],
            idempotency_key="activation-validate-admin",
        ),
        actor,
    )
    activation = ConfigCommand(
        action=ConfigAction.ACTIVATE,
        module="core_admin",
        expected_revision=validated.module["revision"],
        validation_id=validated.validation["validation_id"],
        idempotency_key="initialize-core-admin-v1",
    )
    activated = await service.execute(activation, actor)
    replayed = await service.execute(activation, actor)

    assert activated.system_state == "READY"
    assert replayed == activated
    assert activated.module["draft"] is None
    assert "password" not in activated.module["effective"]["config"]
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.password_state == PasswordState.RESET_REQUIRED
        assert user.password_hash
        assert await authenticate_builtin(session, "admin", "anything") is None
    claim = await repository.get_bootstrap_claim()
    assert claim is not None
    assert claim.consumed_at is not None


async def test_managed_receipt_failure_cannot_leave_activation_committed(
    managed_context,
    monkeypatch,
) -> None:
    factory, repository, service, _ = managed_context
    actor = ConfigActor(
        scope="managed:pmcp-a:1",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=1,
        lease_owner="worker-a",
    )
    saved = await service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="core_admin",
            expected_revision=0,
            idempotency_key="receipt-save-admin",
            config={"username": "admin"},
        ),
        actor,
    )
    validated = await service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="core_admin",
            expected_revision=saved.module["revision"],
            idempotency_key="receipt-validate-admin",
        ),
        actor,
    )

    async def fail_legacy_receipt(**_kwargs):
        raise RuntimeError("legacy receipt path must not run")

    monkeypatch.setattr(repository, "store_receipt", fail_legacy_receipt)

    activated = await service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="core_admin",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="atomic-managed-activation",
        ),
        actor,
    )

    assert activated.system_state == "READY"
    async with factory() as session:
        assert await session.scalar(select(User)) is not None
