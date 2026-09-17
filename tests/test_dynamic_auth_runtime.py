from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

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
    EffectiveConfig,
    ModuleState,
)
from server.core.crypto import encrypt
from server.models import OIDCLoginState, User, UserRole
from tests._configuration_helpers import ROOT_KEY, create_config_context


async def test_runtime_projection_enables_sso_only_when_active() -> None:
    context = await create_config_context()
    try:
        store = RuntimeConfigStore(context.repository, context.crypto)
        await store.poll_once()
        assert store.current().auth.mode == "builtin"
        assert store.current().auth.web_sso_guard.enabled is False
    finally:
        await context.close()


async def test_runtime_projects_token_exchange_without_browser_oidc() -> None:
    context = await create_config_context()
    try:
        document = await context.repository.get_module("user_sso")
        assert document is not None
        document = document.model_copy(
            update={
                "workflow_state": ModuleState.ACTIVE,
                "effective": EffectiveConfig(
                    revision=1,
                    state=ModuleState.ACTIVE,
                    config={
                        "browser_login_enabled": False,
                        "external_token_trust": {
                            "enabled": True,
                            "provider": "oauth2_introspection",
                            "introspection_endpoint": (
                                "https://idp.example/introspect"
                            ),
                            "client_id": "pas-token-validator",
                            "client_secret": "secret",
                        },
                    },
                ),
            }
        )
        await context.repository.compare_and_set_module(
            "user_sso",
            expected_revision=document.revision,
            document=document,
        )
        store = RuntimeConfigStore(context.repository, context.crypto)
        await store.poll_once()

        assert store.current().auth.mode == "builtin"
        assert store.current().auth.oidc.client_id == ""
        assert store.current().auth.external_token_trust.enabled is True
        assert (
            store.current().auth.external_token_trust.introspection_endpoint
            == "https://idp.example/introspect"
        )
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


async def test_activating_sso_increments_session_epoch(monkeypatch) -> None:
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(ROOT_KEY).decode(),
    )
    context = await create_config_context()
    try:
        async with context.repository.session_factory() as session:
            admin = User(
                external_id="session-epoch-admin",
                display_name="Session Epoch Admin",
                role=UserRole.ADMIN,
            )
            session.add(admin)
            await session.commit()
        actor = ConfigActor(
            scope=f"admin:{admin.id}",
            actor_type="admin",
        )
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
        assert validated.validation is not None
        validated_document = await context.repository.get_module("user_sso")
        assert validated_document.last_validation is not None
        sso_test = OIDCLoginState(
            state_hash="a" * 64,
            purpose="config_test",
            initiator_user_id=admin.id,
            config_revision=validated.module["revision"],
            config_digest=(
                validated_document.last_validation.config_digest
            ),
            nonce_ciphertext=encrypt("nonce"),
            identity_snapshot_ciphertext=encrypt(
                json.dumps(
                    {
                        "provider_name": "oidc",
                        "subject": "session-epoch-admin",
                    }
                )
            ),
            status="passed",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        async with context.repository.session_factory() as session:
            session.add(sso_test)
            await session.commit()
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="user_sso",
                expected_revision=validated.module["revision"],
                validation_id=validated.validation["validation_id"],
                sso_test_id=sso_test.id,
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
