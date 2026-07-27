from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.responses import PlainTextResponse

from server.api.configuration import router
from server.auth.jwt_manager import create_access_token, reset_keys
from server.configuration.bootstrap import (
    rotate_bootstrap_token,
    verify_bootstrap_token,
)
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)
from server.core.config_crypto import SecretEnvelope
from server.middleware.runtime_policy import (
    RuntimeAccessPolicy,
    RuntimePolicyMiddleware,
)
from server.models import ConfigBootstrapClaim, ConfigOperationReceipt, User
from tests._configuration_helpers import create_config_context
from tests._helpers import init_test_jwt_keys


ADMIN = ConfigActor(scope="admin:test", actor_type="admin")


async def test_bootstrap_expiry_replay_and_brute_force_lockout() -> None:
    context = await create_config_context()
    try:
        token = await rotate_bootstrap_token(context.repository)
        for _ in range(10):
            assert not await verify_bootstrap_token(
                context.repository, "wrong-token"
            )
        assert not await verify_bootstrap_token(
            context.repository, token
        )

        replacement = await rotate_bootstrap_token(context.repository)
        async with context.repository.session_factory() as session:
            claim = await session.get(ConfigBootstrapClaim, "bootstrap")
            claim.expires_at = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
            await session.commit()
        assert not await verify_bootstrap_token(
            context.repository, replacement
        )

        replay = await rotate_bootstrap_token(context.repository)
        assert await verify_bootstrap_token(context.repository, replay)
        await context.repository.consume_bootstrap_claim()
        assert not await verify_bootstrap_token(
            context.repository, replay
        )
    finally:
        await context.close()


def test_secret_envelope_rejects_ciphertext_and_aad_tampering() -> None:
    from server.core.config_crypto import ConfigCrypto

    crypto = ConfigCrypto(b"01234567890123456789012345678901")
    envelope = crypto.encrypt_field(
        "sensitive-value",
        module="user_sso",
        field_path="client_secret",
        schema_version=1,
    )
    raw = bytearray(base64.b64decode(envelope.ciphertext))
    raw[0] ^= 1
    tampered = envelope.model_copy(
        update={"ciphertext": base64.b64encode(raw).decode("ascii")}
    )

    with pytest.raises(InvalidTag):
        crypto.decrypt_field(
            tampered,
            module="user_sso",
            field_path="client_secret",
            schema_version=1,
        )
    with pytest.raises(InvalidTag):
        crypto.decrypt_field(
            envelope,
            module="aliyun_access",
            field_path="client_secret",
            schema_version=1,
        )


async def test_secret_is_absent_from_response_receipt_and_logs(
    caplog,
) -> None:
    context = await create_config_context()
    secret = "never-print-this-credential"
    caplog.set_level(
        logging.INFO, logger="server.configuration.audit"
    )
    try:
        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=0,
                config={
                    "credential_mode": "direct_ak",
                    "access_key_id": "test-ak",
                    "access_key_secret": secret,
                },
            ),
            ADMIN,
        )
        validated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.VALIDATE,
                module="aliyun_access",
                expected_revision=saved.module["revision"],
            ),
            ADMIN,
        )
        activated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="aliyun_access",
                expected_revision=validated.module["revision"],
                validation_id=validated.validation["validation_id"],
                idempotency_key="activate-aliyun",
            ),
            ADMIN,
        )
        public_output = activated.model_dump_json()
        assert secret not in public_output
        assert "ciphertext" not in public_output
        assert secret not in caplog.text
        audit_records = [
            record
            for record in caplog.records
            if record.name == "server.configuration.audit"
        ]
        assert [record.config_action for record in audit_records] == [
            "save_draft",
            "validate",
            "activate",
        ]
        assert all(
            record.config_module == "aliyun_access"
            and record.config_result == "success"
            for record in audit_records
        )
        assert secret not in repr(
            [record.__dict__ for record in audit_records]
        )
        assert "ciphertext" not in repr(
            [record.__dict__ for record in audit_records]
        )

        async with context.repository.session_factory() as session:
            receipts = (
                await session.execute(select(ConfigOperationReceipt))
            ).scalars().all()
        assert len(receipts) == 1
        assert secret not in receipts[0].response_json
        assert "ciphertext" not in receipts[0].response_json

        internal = await context.service.describe_internal(
            "aliyun_access"
        )
        envelope = SecretEnvelope.model_validate(
            internal.effective.config["access_key_secret"]["$secret"]
        )
        assert secret not in envelope.model_dump_json()
    finally:
        await context.close()


async def test_core_admin_plan_does_not_audit_transient_password(
    caplog,
) -> None:
    context = await create_config_context()
    password = "correct horse battery staple"
    caplog.set_level(
        logging.INFO, logger="server.configuration.audit"
    )
    try:
        result = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.PLAN,
                module="core_admin",
                config={"username": "admin", "password": password},
            ),
            ADMIN,
        )

        assert result.plan["valid"] is True
        audit_record = next(
            record
            for record in caplog.records
            if record.name == "server.configuration.audit"
        )
        assert audit_record.config_changed_fields == ("username",)
        assert password not in caplog.text
        assert password not in repr(audit_record.__dict__)
    finally:
        await context.close()


async def test_dependency_cannot_be_bypassed() -> None:
    context = await create_config_context()
    try:
        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="agentic_db_purchase",
                expected_revision=0,
                config={"enabled": True},
            ),
            ADMIN,
        )
        with pytest.raises(ConfigError) as error:
            await context.service.execute(
                ConfigCommand(
                    action=ConfigAction.VALIDATE,
                    module="agentic_db_purchase",
                    expected_revision=saved.module["revision"],
                ),
                ADMIN,
            )
        assert error.value.code == "DEPENDENCY_NOT_ACTIVE"
    finally:
        await context.close()


async def test_setup_mode_blocks_non_setup_routes() -> None:
    app = FastAPI()

    @app.get("/private")
    async def private() -> PlainTextResponse:
        return PlainTextResponse("private")

    app.add_middleware(
        RuntimePolicyMiddleware,
        snapshot_provider=lambda: RuntimeAccessPolicy(mode="SETUP"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        blocked = await client.get("/private")
        live = await client.get("/livez")
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "SETUP_REQUIRED"
    assert live.status_code == 404


async def test_cookie_configuration_requires_csrf_header() -> None:
    context = await create_config_context()
    reset_keys()
    init_test_jwt_keys()
    try:
        token = await rotate_bootstrap_token(context.repository)
        app = FastAPI()
        app.state.config_service = context.service
        app.include_router(router, prefix="/api")
        bootstrap = {"Authorization": f"Bootstrap {token}"}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            saved = await client.post(
                "/api/config",
                headers=bootstrap,
                json={
                    "protocol_version": 1,
                    "action": "save_draft",
                    "module": "core_admin",
                    "expected_revision": 0,
                    "config": {"username": "admin"},
                },
            )
            validated = await client.post(
                "/api/config",
                headers=bootstrap,
                json={
                    "protocol_version": 1,
                    "action": "validate",
                    "module": "core_admin",
                    "expected_revision": saved.json()["module"][
                        "revision"
                    ],
                },
            )
            activated = await client.post(
                "/api/config",
                headers=bootstrap,
                json={
                    "protocol_version": 1,
                    "action": "activate",
                    "module": "core_admin",
                    "expected_revision": validated.json()["module"][
                        "revision"
                    ],
                    "validation_id": validated.json()["validation"][
                        "validation_id"
                    ],
                    "idempotency_key": "ready",
                    "config": {
                        "password": "correct horse battery staple"
                    },
                },
            )
            assert activated.status_code == 200
            async with context.repository.session_factory() as session:
                admin = (
                    await session.execute(select(User))
                ).scalar_one()
            session_token = create_access_token(
                {"sub": admin.id, "role": "admin"}
            )
            client.cookies.set("session_token", session_token)

            rejected = await client.post(
                "/api/config",
                json={
                    "protocol_version": 1,
                    "action": "describe",
                },
            )
            accepted = await client.post(
                "/api/config",
                headers={"X-PAS-CSRF": "1"},
                json={
                    "protocol_version": 1,
                    "action": "describe",
                },
            )
            bearer = await client.post(
                "/api/config",
                headers={"Authorization": f"Bearer {session_token}"},
                json={
                    "protocol_version": 1,
                    "action": "describe",
                },
            )
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["code"] == "CSRF_REQUIRED"
        assert accepted.status_code == 200
        assert bearer.status_code == 200
    finally:
        reset_keys()
        await context.close()
