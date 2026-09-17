from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.builtin import authenticate_builtin
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
from server.models import (
    Base,
    ConfigOperationReceipt,
    PasswordState,
    User,
    UserRefreshToken,
)
from server.management.app import create_management_app
from server.management.settings import (
    ListenerSettings,
    ManagedIdentitySettings,
)
from server.runtime import RuntimePhase


ROOT_KEY = b"01234567890123456789012345678901"
NEW_PASSWORD = "new-managed-password"


@pytest.fixture
async def reset_required_context(tmp_path):
    database = tmp_path / "initial-password.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    await initialize_configuration(repository, crypto, managed=True)
    await repository.bind_managed_identity(
        instance_id="pmcp-a",
        generation=1,
    )
    service = ConfigService(repository, crypto)
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
            idempotency_key="fixture-save-admin",
            config={"username": "admin"},
        ),
        actor,
    )
    validated = await service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="core_admin",
            expected_revision=saved.module["revision"],
            idempotency_key="fixture-validate-admin",
        ),
        actor,
    )
    await service.execute(
        ConfigCommand(
            action=ConfigAction.ACTIVATE,
            module="core_admin",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="activate-admin",
        ),
        actor,
    )
    yield factory, repository, service, actor
    await engine.dispose()


def _password_command(
    *,
    password: str = NEW_PASSWORD,
    idempotency_key: str = "set-initial-password",
) -> ConfigCommand:
    return ConfigCommand(
        action=ConfigAction.SET_INITIAL_PASSWORD,
        module="core_admin",
        config={"password": password},
        idempotency_key=idempotency_key,
    )


async def test_initial_password_is_atomic_replayable_and_secret_safe(
    reset_required_context,
    caplog,
) -> None:
    factory, repository, service, actor = reset_required_context
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    command = _password_command()

    before = None
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        before = await authenticate_builtin(session, "admin", NEW_PASSWORD)
        session.add(
            UserRefreshToken(
                user_id=user.id,
                token_hash="initial-password-token",
                token_family="initial-password-family",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        await session.commit()
    result = await service.execute(command, actor)
    replay = await service.execute(command, actor)

    assert before is None
    assert replay == result
    assert result.system_state == "READY"
    assert result.credential_state == PasswordState.ACTIVE
    assert NEW_PASSWORD not in result.model_dump_json()
    assert NEW_PASSWORD not in caplog.text
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.password_state == PasswordState.ACTIVE
        assert user.credential_epoch == 2
        assert await authenticate_builtin(session, "admin", NEW_PASSWORD) is not None
        refresh_token = await session.scalar(
            select(UserRefreshToken).where(
                UserRefreshToken.token_hash == "initial-password-token"
            )
        )
        assert refresh_token is not None
        assert refresh_token.revoked_at is not None
        receipts = (await session.scalars(select(ConfigOperationReceipt))).all()
        assert all(NEW_PASSWORD not in receipt.response_json for receipt in receipts)
        assert all(NEW_PASSWORD not in receipt.request_digest for receipt in receipts)


async def test_management_api_sets_password_and_reports_live_state(
    reset_required_context,
) -> None:
    _, _, service, _ = reset_required_context
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(
            config_service=service,
            runtime_access_policy=SimpleNamespace(mode="READY"),
        ),
    )
    settings = ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="trusted-network",
        token=None,
        managed_identity=ManagedIdentitySettings("pmcp-a", 1),
    )
    app = create_management_app(runtime, settings)
    body = {
        "protocol_version": 1,
        "target": {"instance_id": "pmcp-a", "generation": 1},
        "actor": {"type": "managed_initializer", "subject_id": "task-a"},
        "command": {
            "module": "core_admin",
            "action": "set_initial_password",
            "parameter": "password",
            "value": NEW_PASSWORD,
            "idempotency_key": "management-api-password",
        },
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status_before = await client.get("/api/internal/v1/status")
        response = await client.post("/api/internal/v1/config", json=body)
        status = await client.get("/api/internal/v1/status")

    assert status_before.status_code == 200
    assert status_before.json()["credential_state"] == "RESET_REQUIRED"
    assert response.status_code == 200
    assert response.json()["credential_state"] == "ACTIVE"
    assert NEW_PASSWORD not in response.text
    assert status.status_code == 200
    assert status.json()["credential_state"] == "ACTIVE"


async def test_management_api_rejects_wrong_generation_without_password_leak(
    reset_required_context,
) -> None:
    _, _, service, _ = reset_required_context
    runtime = SimpleNamespace(
        phase=RuntimePhase.READY,
        error_code=None,
        application_state=SimpleNamespace(config_service=service),
    )
    settings = ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="trusted-network",
        token=None,
        managed_identity=ManagedIdentitySettings("pmcp-a", 1),
    )
    app = create_management_app(runtime, settings)
    body = {
        "protocol_version": 1,
        "target": {"instance_id": "pmcp-a", "generation": 2},
        "actor": {"type": "managed_initializer"},
        "command": {
            "module": "core_admin",
            "action": "set_initial_password",
            "parameter": "password",
            "value": NEW_PASSWORD,
            "idempotency_key": "wrong-generation",
        },
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/internal/v1/config", json=body)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INSTANCE_GENERATION_MISMATCH"
    assert NEW_PASSWORD not in response.text


async def test_weak_initial_password_is_rejected_without_secret_leak(
    reset_required_context,
    caplog,
) -> None:
    factory, _, service, actor = reset_required_context
    weak_password = "too-short"
    caplog.set_level(logging.INFO, logger="server.configuration.audit")

    with pytest.raises(ConfigError) as captured:
        await service.execute(_password_command(password=weak_password), actor)

    assert captured.value.code == "INVALID_ADMIN_PASSWORD"
    assert weak_password not in str(captured.value)
    assert weak_password not in caplog.text
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.password_state == PasswordState.RESET_REQUIRED


async def test_wrong_generation_cannot_initialize_password(
    reset_required_context,
) -> None:
    factory, _, service, _ = reset_required_context
    wrong_actor = ConfigActor(
        scope="managed:pmcp-a:2",
        actor_type="managed_initializer",
        instance_id="pmcp-a",
        instance_generation=2,
        lease_owner="worker-b",
    )

    with pytest.raises(ConfigError) as captured:
        await service.execute(_password_command(), wrong_actor)

    assert captured.value.code == "INSTANCE_GENERATION_MISMATCH"
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.password_state == PasswordState.RESET_REQUIRED


async def test_new_operation_after_initialization_is_rejected(
    reset_required_context,
) -> None:
    _, _, service, actor = reset_required_context
    await service.execute(_password_command(), actor)

    with pytest.raises(ConfigError) as captured:
        await service.execute(
            _password_command(
                password="another-managed-password",
                idempotency_key="another-operation",
            ),
            actor,
        )

    assert captured.value.code == "ADMIN_PASSWORD_ALREADY_INITIALIZED"

    with pytest.raises(ConfigError) as retried:
        await service.execute(
            _password_command(
                password="another-managed-password",
                idempotency_key="another-operation",
            ),
            actor,
        )
    assert retried.value.code == "ADMIN_PASSWORD_ALREADY_INITIALIZED"

    async with reset_required_context[0]() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.credential_epoch == 2


async def test_same_operation_key_with_another_password_conflicts_safely(
    reset_required_context,
) -> None:
    _, _, service, actor = reset_required_context
    await service.execute(_password_command(), actor)
    other_password = "different-managed-password"

    with pytest.raises(ConfigError) as captured:
        await service.execute(
            _password_command(password=other_password),
            actor,
        )

    assert captured.value.code == "IDEMPOTENCY_CONFLICT"
    assert other_password not in str(captured.value)


async def test_concurrent_initial_password_calls_have_one_transition(
    reset_required_context,
) -> None:
    factory, _, service, actor = reset_required_context
    other_actor = actor.model_copy(update={"lease_owner": "worker-b"})
    command = _password_command(idempotency_key="concurrent-operation")

    outcomes = await asyncio.gather(
        service.execute(command, actor),
        service.execute(command, other_actor),
        return_exceptions=True,
    )

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert successes
    assert all(
        isinstance(outcome, ConfigError) and outcome.code == "OPERATION_IN_PROGRESS"
        for outcome in failures
    )
    replay = await service.execute(command, other_actor)
    assert replay == successes[0]
    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.password_state == PasswordState.ACTIVE


async def test_password_transition_rolls_back_if_receipt_completion_fails(
    reset_required_context,
    monkeypatch,
) -> None:
    factory, repository, service, actor = reset_required_context
    real_transition = repository.set_initial_admin_password_in_session

    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        old_hash = user.password_hash
        session.add(
            UserRefreshToken(
                user_id=user.id,
                token_hash="rollback-password-token",
                token_family="rollback-password-family",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        await session.commit()

    async def fail_after_transition(session, **kwargs):
        await real_transition(session, **kwargs)
        raise RuntimeError("simulated receipt failure")

    monkeypatch.setattr(
        repository,
        "set_initial_admin_password_in_session",
        fail_after_transition,
    )

    with pytest.raises(RuntimeError, match="simulated receipt failure"):
        await service.execute(
            _password_command(idempotency_key="rollback-operation"), actor
        )

    async with factory() as session:
        user = await session.scalar(select(User).where(User.external_id == "admin"))
        assert user is not None
        assert user.password_state == PasswordState.RESET_REQUIRED
        assert user.password_hash == old_hash
        assert user.credential_epoch == 1
        refresh_token = await session.scalar(
            select(UserRefreshToken).where(
                UserRefreshToken.token_hash == "rollback-password-token"
            )
        )
        assert refresh_token is not None
        assert refresh_token.revoked_at is None
