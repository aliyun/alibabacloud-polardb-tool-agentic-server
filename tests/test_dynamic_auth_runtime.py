from __future__ import annotations

import pytest

from server.auth.jwt_manager import (
    create_access_token,
    initialize_jwt_keys_from_db,
    reset_keys,
    verify_token,
)
from server.configuration.runtime import RuntimeConfigStore
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
)
from tests._configuration_helpers import create_config_context


async def test_runtime_projection_enables_sso_only_when_active() -> None:
    context = await create_config_context()
    try:
        store = RuntimeConfigStore(context.repository, context.crypto)
        await store.poll_once()
        assert store.current().auth.mode == "builtin"
        assert store.current().auth.web_sso_guard.enabled is False
    finally:
        await context.close()


async def test_session_epoch_invalidates_human_jwt() -> None:
    context = await create_config_context()
    try:
        async with context.repository.session_factory() as session:
            await initialize_jwt_keys_from_db(session, context.crypto)
        old_token = create_access_token({"sub": "user-1"})
        token_security = await context.repository.get_module(
            "token_security"
        )
        token_security.effective.config["session_epoch"] = 2
        token_security.effective.revision += 1
        await context.repository.compare_and_set_module(
            "token_security",
            expected_revision=token_security.revision,
            document=token_security,
        )
        reset_keys()
        async with context.repository.session_factory() as session:
            await initialize_jwt_keys_from_db(session, context.crypto)

        from jwt import PyJWTError

        try:
            verify_token(old_token)
        except PyJWTError:
            pass
        else:
            raise AssertionError("stale human token was accepted")
    finally:
        reset_keys()
        await context.close()


async def test_activating_sso_increments_session_epoch() -> None:
    context = await create_config_context()
    actor = ConfigActor(scope="admin:1", actor_type="admin")
    try:
        runtime = await context.repository.get_module("runtime_policy")
        runtime.effective.config["external_base_url"] = (
            "https://pas.example.com"
        )
        runtime.effective.revision += 1
        await context.repository.compare_and_set_module(
            "runtime_policy",
            expected_revision=runtime.revision,
            document=runtime,
        )
        async with context.repository.session_factory() as session:
            await initialize_jwt_keys_from_db(session, context.crypto)
        old_token = create_access_token({"sub": "user-1"})

        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="user_sso",
                expected_revision=0,
                config={
                    "client_id": "client",
                    "client_secret": "secret",
                },
            ),
            actor,
        )
        validated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.VALIDATE,
                module="user_sso",
                expected_revision=saved.module["revision"],
            ),
            actor,
        )
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="user_sso",
                expected_revision=validated.module["revision"],
                validation_id=validated.validation["validation_id"],
                idempotency_key="activate-sso",
            ),
            actor,
        )
        reset_keys()
        async with context.repository.session_factory() as session:
            await initialize_jwt_keys_from_db(session, context.crypto)

        from jwt import PyJWTError

        with pytest.raises(PyJWTError):
            verify_token(old_token)
    finally:
        reset_keys()
        await context.close()
