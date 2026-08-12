from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from server.configuration.service import ConfigService
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
    ModuleState,
    ValidationOperation,
)
from server.configuration.external_validation import ExternalValidationError
from tests._configuration_helpers import create_config_context

ADMIN = ConfigActor(scope="admin:1", actor_type="admin")


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


def command(action: ConfigAction, module: str, **values) -> ConfigCommand:
    return ConfigCommand(action=action, module=module, **values)


async def test_save_draft_describe_and_reset(context) -> None:
    service = context.service
    described = await service.execute(
        command(ConfigAction.DESCRIBE, "agent_token_auth"),
        ADMIN,
    )
    assert described.module["workflow_state"] == ModuleState.ACTIVE
    assert described.module["configurable"] is False
    assert described.module["schema"]["type"] == "object"

    for action in (
        ConfigAction.SAVE_DRAFT,
        ConfigAction.PLAN,
        ConfigAction.VALIDATE,
        ConfigAction.ACTIVATE,
        ConfigAction.RESET,
    ):
        with pytest.raises(ConfigError) as error:
            await service.execute(
                command(
                    action,
                    "agent_token_auth",
                    expected_revision=1,
                    config={"enabled": True},
                    idempotency_key=(
                        "agent-token-built-in"
                        if action == ConfigAction.ACTIVATE
                        else None
                    ),
                ),
                ADMIN,
            )
        assert error.value.code == "MODULE_NOT_CONFIGURABLE"

    for action in (ConfigAction.SKIP, ConfigAction.DISABLE):
        with pytest.raises(ConfigError) as error:
            await service.execute(
                command(
                    action,
                    "agent_token_auth",
                    expected_revision=1,
                    idempotency_key=(
                        "agent-token-required"
                        if action == ConfigAction.DISABLE
                        else None
                    ),
                ),
                ADMIN,
            )
        assert error.value.code == "MODULE_NOT_CONFIGURABLE"


async def test_resource_pool_configuration_is_retired(context) -> None:
    with pytest.raises(ConfigError) as error:
        await context.service.execute(
            command(ConfigAction.DESCRIBE, "resource_pool"),
            ADMIN,
        )

    assert error.value.code == "UNKNOWN_MODULE"


@pytest.mark.parametrize(
    "action",
    [
        ConfigAction.DESCRIBE,
        ConfigAction.PLAN,
        ConfigAction.SAVE_DRAFT,
        ConfigAction.VALIDATE,
        ConfigAction.ACTIVATE,
        ConfigAction.SKIP,
        ConfigAction.DISABLE,
        ConfigAction.RESET,
        ConfigAction.EXPORT,
    ],
)
async def test_agentic_purchase_configuration_is_retired(
    context, action: ConfigAction
) -> None:
    values = {"config": {}}
    if action in {ConfigAction.ACTIVATE, ConfigAction.DISABLE}:
        values["idempotency_key"] = f"retired-{action.value}"

    with pytest.raises(ConfigError) as error:
        await context.service.execute(
            command(action, "agentic_db_purchase", **values),
            ADMIN,
        )

    assert error.value.code == "UNKNOWN_MODULE"


async def test_plan_is_dry_run_and_does_not_write(context) -> None:
    before = await context.repository.global_version()
    result = await context.service.execute(
        command(
            ConfigAction.PLAN,
            "aliyun_access",
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "ak",
                    "access_key_secret": "secret",
                },
            },
        ),
        ADMIN,
    )
    assert result.plan["valid"] is True
    assert result.plan["config"]["direct_ak"]["access_key_secret"][
        "configured"
    ]
    assert await context.repository.global_version() == before


@pytest.mark.parametrize("action", [ConfigAction.PLAN, ConfigAction.VALIDATE])
async def test_command_exception_overrides_preset_aliyun_audit_outcome(
    context, caplog, monkeypatch, action: ConfigAction
) -> None:
    if action == ConfigAction.VALIDATE:
        saved = await context.service.execute(
            command(
                ConfigAction.SAVE_DRAFT,
                "aliyun_access",
                expected_revision=0,
                config={
                    "credential_mode": "direct_ak",
                    "direct_ak": {
                        "access_key_id": "TEST1234567890ABCD",
                        "access_key_secret": "fixed-secret",
                    },
                },
            ),
            ADMIN,
        )
        values = {"expected_revision": saved.module["revision"]}
    else:
        values = {
            "config": {
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "TEST1234567890ABCD",
                    "access_key_secret": "fixed-secret",
                },
            }
        }

    async def fail_result(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("fixed-secret result construction failure")

    monkeypatch.setattr(context.service, "_result", fail_result)
    caplog.set_level(logging.INFO, logger="server.configuration.audit")
    caplog.clear()

    with pytest.raises(RuntimeError):
        await context.service.execute(
            command(action, "aliyun_access", **values), ADMIN
        )

    record = caplog.records[-1]
    assert record.config_result == "error"
    assert record.config_error_code == "INTERNAL_ERROR"
    assert "fixed-secret" not in repr(record.__dict__)


async def test_confirmed_external_failure_can_activate_with_warning(
    context,
) -> None:
    class FailingValidator:
        async def validate(self, module, config):
            del module, config
            raise ExternalValidationError(
                "OPENAPI_CONNECT_FAILURE",
                "sanitized connection failure",
                request_id="safe-request:789",
            )

    service = ConfigService(
        context.repository, context.crypto, external_validator=FailingValidator()
    )
    saved = await service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "aliyun_access",
            expected_revision=0,
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "test-ak",
                    "access_key_secret": "test-secret",
                },
            },
        ),
        ADMIN,
    )

    planned = await service.execute(
        command(
            ConfigAction.PLAN,
            "aliyun_access",
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "test-ak",
                    "access_key_secret": "test-secret",
                },
            },
        ),
        ADMIN,
    )
    assert planned.plan["confirmation_allowed"] is True
    assert planned.plan["confirmation_error_code"] == "OPENAPI_CONNECT_FAILURE"
    assert planned.plan["request_id"] == "safe-request:789"

    validated = await service.execute(
        command(
            ConfigAction.VALIDATE,
            "aliyun_access",
            expected_revision=saved.module["revision"],
            confirm_impact=True,
        ),
        ADMIN,
    )

    assert validated.validation["warnings"] == ["OPENAPI_CONNECT_FAILURE"]
    assert validated.validation["validation_id"]
    assert validated.validation["request_id"] == "safe-request:789"
    document = await context.repository.get_module("aliyun_access")
    assert document.last_validation is not None
    assert document.last_validation.request_id == "safe-request:789"

    activated = await service.execute(
        command(
            ConfigAction.ACTIVATE,
            "aliyun_access",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="activate-confirmed-warning",
        ),
        ADMIN,
    )
    assert activated.module["workflow_state"] == ModuleState.ACTIVE
    consumed = await context.repository.get_module("aliyun_access")
    assert consumed.last_validation is None


async def test_endpoint_unsupported_cannot_be_confirmed(context) -> None:
    class UnsupportedValidator:
        async def validate(self, module, config):
            del module, config
            raise ExternalValidationError(
                "OPENAPI_ENDPOINT_UNSUPPORTED", "unsupported endpoint"
            )

    service = ConfigService(
        context.repository, context.crypto, external_validator=UnsupportedValidator()
    )
    saved = await service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "aliyun_access",
            expected_revision=0,
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "test-ak",
                    "access_key_secret": "test-secret",
                },
            },
        ),
        ADMIN,
    )

    planned = await service.execute(
        command(
            ConfigAction.PLAN,
            "aliyun_access",
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "test-ak",
                    "access_key_secret": "test-secret",
                },
            },
        ),
        ADMIN,
    )
    assert planned.plan["confirmation_allowed"] is False

    with pytest.raises(ConfigError) as error:
        await service.execute(
            command(
                ConfigAction.VALIDATE,
                "aliyun_access",
                expected_revision=saved.module["revision"],
                confirm_impact=True,
            ),
            ADMIN,
        )
    assert error.value.code == "OPENAPI_ENDPOINT_UNSUPPORTED"


async def test_confirm_impact_never_bypasses_a_dependency_failure(
    context, monkeypatch
) -> None:
    validator = AsyncMock()
    service = ConfigService(
        context.repository, context.crypto, external_validator=validator
    )
    saved = await service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "aliyun_access",
            expected_revision=0,
            config={
                "credential_mode": "direct_ak",
                "direct_ak": {
                    "access_key_id": "test-ak",
                    "access_key_secret": "test-secret",
                },
            },
        ),
        ADMIN,
    )

    async def fail_dependency(module: str) -> dict[str, int]:
        del module
        raise ConfigError("DEPENDENCY_NOT_ACTIVE", "dependency is inactive")

    monkeypatch.setattr(service, "_dependency_revisions", fail_dependency)

    with pytest.raises(ConfigError, match="dependency is inactive") as error:
        await service.execute(
            command(
                ConfigAction.VALIDATE,
                "aliyun_access",
                expected_revision=saved.module["revision"],
                confirm_impact=True,
            ),
            ADMIN,
        )

    assert error.value.code == "DEPENDENCY_NOT_ACTIVE"
    validator.validate.assert_not_awaited()


@pytest.mark.parametrize(
    ("password", "valid", "error_code"),
    [
        ("too-short", False, "INVALID_ADMIN_PASSWORD"),
        ("correct horse battery staple", True, None),
    ],
)
async def test_core_admin_plan_checks_transient_password_without_writing(
    context,
    password: str,
    valid: bool,
    error_code: str | None,
) -> None:
    before_version = await context.repository.global_version()

    result = await context.service.execute(
        command(
            ConfigAction.PLAN,
            "core_admin",
            config={"username": "admin", "password": password},
        ),
        ADMIN,
    )

    assert result.plan["valid"] is valid
    assert result.plan["error_code"] == error_code
    assert result.plan["writes"] is False
    assert password not in result.model_dump_json()
    assert "password" not in result.plan["config"]
    document = await context.repository.get_module("core_admin")
    assert document.revision == 0
    assert document.draft is None
    assert await context.repository.global_version() == before_version


async def test_validation_proof_survives_service_instance(context) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "observability",
            expected_revision=1,
            config={"log_level": "debug"},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "observability",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    other = ConfigService(context.repository, context.crypto)
    activated = await other.execute(
        command(
            ConfigAction.ACTIVATE,
            "observability",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="activate-observability",
        ),
        ADMIN,
    )
    assert activated.module["workflow_state"] == ModuleState.ACTIVE
    assert activated.module["effective"]["config"]["log_level"] == "debug"


async def test_editing_validated_draft_invalidates_proof(context) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "observability",
            expected_revision=1,
            config={"log_level": "debug"},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "observability",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    validation_id = validated.validation["validation_id"]
    edited = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "observability",
            expected_revision=validated.module["revision"],
            config={"log_level": "warning"},
        ),
        ADMIN,
    )
    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            command(
                ConfigAction.ACTIVATE,
                "observability",
                expected_revision=edited.module["revision"],
                validation_id=validation_id,
                idempotency_key="stale-proof",
            ),
            ADMIN,
        )
    assert exc.value.code == "VALIDATION_STALE"


async def test_expired_proof_returns_module_to_draft(context) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "observability",
            expected_revision=1,
            config={"log_level": "debug"},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "observability",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    document = await context.repository.get_module("observability")
    document.last_validation.expires_at = datetime.now(
        timezone.utc
    ) - timedelta(seconds=1)
    await context.repository.compare_and_set_module(
        "observability",
        expected_revision=validated.module["revision"],
        document=document,
    )

    described = await context.service.execute(
        command(ConfigAction.DESCRIBE, "observability"),
        ADMIN,
    )
    assert described.module["workflow_state"] == ModuleState.DRAFT
    assert described.module["last_validation"] is None


async def test_expired_validation_lease_is_recovered(context) -> None:
    document = await context.repository.get_module("observability")
    document.workflow_state = ModuleState.VALIDATING
    document.validation_operation = ValidationOperation(
        operation_id="abandoned",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=3),
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    await context.repository.compare_and_set_module(
        "observability",
        expected_revision=1,
        document=document,
    )

    recovered = await context.service.describe_internal("observability")
    assert recovered.workflow_state == ModuleState.DRAFT
    assert recovered.last_error_code == "VALIDATION_INTERRUPTED"


async def test_validation_proof_replay_with_new_key_is_rejected(
    context,
) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "observability",
            expected_revision=1,
            config={"log_level": "debug"},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "observability",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    activation = command(
        ConfigAction.ACTIVATE,
        "observability",
        expected_revision=validated.module["revision"],
        validation_id=validated.validation["validation_id"],
        idempotency_key="first-activation",
    )
    activated = await context.service.execute(activation, ADMIN)
    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            activation.model_copy(
                update={
                    "expected_revision": activated.module["revision"],
                    "idempotency_key": "proof-replay",
                }
            ),
            ADMIN,
        )
    assert exc.value.code == "VALIDATION_STALE"


async def test_dependency_revision_change_invalidates_proof(context) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "core_admin",
            expected_revision=0,
            config={"username": "admin"},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "core_admin",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    token_security = await context.repository.get_module(
        "token_security"
    )
    token_security.effective.revision += 1
    await context.repository.compare_and_set_module(
        "token_security",
        expected_revision=token_security.revision,
        document=token_security,
    )

    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            command(
                ConfigAction.ACTIVATE,
                "core_admin",
                expected_revision=validated.module["revision"],
                validation_id=validated.validation["validation_id"],
                idempotency_key="dependency-changed",
            ),
            ADMIN,
        )
    assert exc.value.code == "VALIDATION_STALE"


async def test_disable_rejects_active_dependent(context) -> None:
    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            command(
                ConfigAction.DISABLE,
                "token_security",
                expected_revision=1,
                idempotency_key="disable-system-module",
            ),
            ADMIN,
        )
    assert exc.value.code == "MODULE_NOT_OPTIONAL"
