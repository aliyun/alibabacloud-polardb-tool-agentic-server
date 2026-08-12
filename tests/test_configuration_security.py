from __future__ import annotations

import base64
import json
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
from server.configuration.external_validation import (
    ExternalValidationCheck,
    ExternalValidationError,
    ExternalValidationResult,
)
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
)
from server.configuration.runtime import _decrypt_effective, project_app_config
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
                    "direct_ak": {
                        "access_key_id": "test-ak",
                        "access_key_secret": secret,
                    },
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
            internal.effective.config["direct_ak"]["access_key_secret"][
                "$secret"
            ]
        )
        assert secret not in envelope.model_dump_json()
        assert _decrypt_effective(
            "aliyun_access", internal, context.crypto
        )["direct_ak"]["access_key_secret"] == secret
        assert project_app_config(
            {"aliyun_access": internal}, context.crypto
        ).aliyun.access_key_secret == secret
    finally:
        await context.close()


async def test_aliyun_transition_audit_uses_only_safe_metadata(caplog) -> None:
    context = await create_config_context()
    secret = "fixed-secret"
    external_id = "customer-external-id"
    role_arn = "acs:ram::123456789012:role/polardb"
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    try:
        initial = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=0,
                config={
                    "credential_mode": "direct_ak",
                    "direct_ak": {
                        "access_key_id": "TEST1234567890ABCD",
                        "access_key_secret": secret,
                    },
                },
            ),
            ADMIN,
        )
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=initial.module["revision"],
                config={
                    "credential_mode": "assume_role",
                    "transition": {"previous_mode_action": "retain"},
                    "assume_role": {
                        "source_access_key_id": "TEST0987654321DCBA",
                        "source_access_key_secret": secret,
                        "role_arn": role_arn,
                        "role_session_name": "polardb-agentic",
                        "external_id": external_id,
                    },
                },
            ),
            ADMIN,
        )

        record = caplog.records[-1]
        assert record.aliyun_previous_mode == "direct_ak"
        assert record.aliyun_selected_mode == "assume_role"
        assert record.aliyun_previous_mode_action == "retain"
        assert record.aliyun_selected_mode_action == "replace"
        assert record.aliyun_credential_id_mask == "TEST****DCBA"
        assert record.config_revision == 2
        payload = json.dumps(record.__dict__, default=str)
        for forbidden in (secret, external_id, role_arn, "123456789012"):
            assert forbidden not in payload
    finally:
        await context.close()


async def test_aliyun_transition_audit_masks_short_access_key_ids(caplog) -> None:
    context = await create_config_context()
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    try:
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=0,
                config={
                    "credential_mode": "direct_ak",
                    "direct_ak": {
                        "access_key_id": "ak",
                        "access_key_secret": "fixed-secret",
                    },
                },
            ),
            ADMIN,
        )

        record = caplog.records[-1]
        assert record.aliyun_credential_id_mask == "****"
        assert '"ak"' not in json.dumps(record.__dict__, default=str)
    finally:
        await context.close()


async def test_aliyun_plan_audit_uses_validation_outcome_and_safe_request_id(
    caplog,
) -> None:
    class FailingValidator:
        async def validate(self, module, config):
            del module, config
            raise ExternalValidationError(
                "OPENAPI_CONNECT_FAILURE",
                "fixed-secret validation message",
                request_id="safe-request-id",
            )

    class SuccessfulValidator:
        async def validate(self, module, config):
            del module, config
            return ExternalValidationResult(
                status="PASSED",
                checks=(
                    ExternalValidationCheck(
                        service="polardb",
                        network="public",
                        endpoint="polardb.aliyuncs.com",
                        status="PASSED",
                        request_id="safe-nested-request-id",
                    ),
                ),
            )

    context = await create_config_context()
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    command = ConfigCommand(
        action=ConfigAction.PLAN,
        module="aliyun_access",
        config={
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "TEST1234567890ABCD",
                "access_key_secret": "fixed-secret",
            },
        },
    )
    try:
        context.service.external_validator = FailingValidator()
        failed = await context.service.execute(command, ADMIN)
        failed_record = caplog.records[-1]
        assert failed.plan["valid"] is False
        assert failed_record.config_result == "error"
        assert failed_record.config_error_code == "OPENAPI_CONNECT_FAILURE"
        assert failed_record.config_request_id == "safe-request-id"

        context.service.external_validator = SuccessfulValidator()
        succeeded = await context.service.execute(command, ADMIN)
        succeeded_record = caplog.records[-1]
        assert succeeded.plan["valid"] is True
        assert succeeded_record.config_result == "success"
        assert succeeded_record.config_request_id == "safe-nested-request-id"
        assert "fixed-secret" not in json.dumps(
            succeeded_record.__dict__, default=str
        )
    finally:
        await context.close()


@pytest.mark.parametrize(
    ("request_id", "expected_request_id"),
    [
        ("safe-validate-request:123", "safe-validate-request:123"),
        ("hostile request id access_key_secret=leak", None),
    ],
)
async def test_unconfirmed_aliyun_validate_failure_audits_only_safe_context(
    caplog,
    request_id: str,
    expected_request_id: str | None,
) -> None:
    access_key_id = "TEST1234567890ABCD"
    access_key_secret = "validate-access-key-secret"
    external_id = "validate-external-id"
    security_token = "validate-security-token"

    class FailingValidator:
        async def validate(self, module, config):
            del module, config
            raise ExternalValidationError(
                "OPENAPI_PERMISSION_DENIED",
                (
                    f"SDK body {access_key_id} {access_key_secret} "
                    f"{external_id} {security_token}"
                ),
                request_id=request_id,
            )

    context = await create_config_context()
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    try:
        context.service.external_validator = FailingValidator()
        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=0,
                config={
                    "credential_mode": "assume_role",
                    "assume_role": {
                        "source_access_key_id": access_key_id,
                        "source_access_key_secret": access_key_secret,
                        "role_arn": "acs:ram::123456789012:role/pas-runtime",
                        "external_id": external_id,
                    },
                },
            ),
            ADMIN,
        )
        caplog.clear()

        with pytest.raises(ConfigError) as error:
            await context.service.execute(
                ConfigCommand(
                    action=ConfigAction.VALIDATE,
                    module="aliyun_access",
                    expected_revision=saved.module["revision"],
                ),
                ADMIN,
            )

        assert error.value.code == "OPENAPI_PERMISSION_DENIED"
        record = caplog.records[-1]
        assert record.config_action == ConfigAction.VALIDATE
        assert record.config_result == "error"
        assert record.config_error_code == "OPENAPI_PERMISSION_DENIED"
        assert record.config_request_id == expected_request_id
        payload = json.dumps(record.__dict__, default=str)
        for forbidden in (
            access_key_id,
            access_key_secret,
            external_id,
            security_token,
            "SDK body",
        ):
            assert forbidden not in payload
    finally:
        await context.close()


async def test_nested_aliyun_values_are_trimmed_before_runtime_projection() -> None:
    context = await create_config_context()
    try:
        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="aliyun_access",
                expected_revision=0,
                config={
                    "credential_mode": " assume_role ",
                    "region_id": " cn-beijing ",
                    "assume_role": {
                        "source_access_key_id": " TEST1234567890ABCD ",
                        "source_access_key_secret": " source-secret ",
                        "role_arn": " acs:ram::123456789012:role/polardb ",
                        "role_session_name": " polardb-agentic ",
                        "external_id": " external-identity ",
                    },
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
        await context.service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="aliyun_access",
                expected_revision=validated.module["revision"],
                validation_id=validated.validation["validation_id"],
                idempotency_key="activate-trimmed-aliyun",
            ),
            ADMIN,
        )

        internal = await context.service.describe_internal("aliyun_access")
        runtime = project_app_config(
            {"aliyun_access": internal}, context.crypto
        ).aliyun
        assert runtime.region_id == "cn-beijing"
        assert runtime.access_key_id == "TEST1234567890ABCD"
        assert runtime.access_key_secret == "source-secret"
        assert runtime.role_arn == "acs:ram::123456789012:role/polardb"
        assert runtime.role_session_name == "polardb-agentic"
        assert internal.effective.config["assume_role"]["role_arn"] == (
            "acs:ram::123456789012:role/polardb"
        )
        stored_secret = internal.effective.config["assume_role"][
            "source_access_key_secret"
        ]
        assert "$secret" in stored_secret
        assert " source-secret " not in internal.model_dump_json()
        assert context.crypto.decrypt_field(
            SecretEnvelope.model_validate(stored_secret["$secret"]),
            module="aliyun_access",
            field_path="assume_role.source_access_key_secret",
            schema_version=2,
        ) == "source-secret"
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
