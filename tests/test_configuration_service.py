from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    saved = await service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "agent_token_auth",
            expected_revision=0,
            config={"enabled": False},
        ),
        ADMIN,
    )
    assert saved.module["workflow_state"] == ModuleState.DRAFT
    assert saved.module["draft"] == {"enabled": False}

    described = await service.execute(
        command(ConfigAction.DESCRIBE, "agent_token_auth"),
        ADMIN,
    )
    assert described.module["schema"]["type"] == "object"

    reset = await service.execute(
        command(
            ConfigAction.RESET,
            "agent_token_auth",
            expected_revision=1,
        ),
        ADMIN,
    )
    assert reset.module["workflow_state"] == ModuleState.DRAFT
    assert reset.module["draft"] is None


async def test_save_draft_trims_surrounding_whitespace(context) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "resource_pool",
            expected_revision=0,
            config={
                "region_id": " cn-hangzhou ",
                "zone_id": "cn-hangzhou-j\t",
                "vpc_id": " vpc-bp14wguf5kh994ffcswa2",
                "vswitch_id": "vsw-bp1u1jhigll0uqwvuoy92 ",
            },
        ),
        ADMIN,
    )
    assert saved.module["draft"] == {
        "region_id": "cn-hangzhou",
        "zone_id": "cn-hangzhou-j",
        "vpc_id": "vpc-bp14wguf5kh994ffcswa2",
        "vswitch_id": "vsw-bp1u1jhigll0uqwvuoy92",
    }


async def test_plan_is_dry_run_and_does_not_write(context) -> None:
    before = await context.repository.global_version()
    result = await context.service.execute(
        command(
            ConfigAction.PLAN,
            "aliyun_access",
            config={
                "credential_mode": "direct_ak",
                "access_key_id": "ak",
                "access_key_secret": "secret",
            },
        ),
        ADMIN,
    )
    assert result.plan["valid"] is True
    assert result.plan["config"]["access_key_secret"] == {
        "configured": True
    }
    assert await context.repository.global_version() == before


async def test_plan_rejects_inactive_dependency_without_writing(
    context,
) -> None:
    before = await context.repository.global_version()

    result = await context.service.execute(
        command(
            ConfigAction.PLAN,
            "agentic_db_purchase",
            config={},
        ),
        ADMIN,
    )

    assert result.plan["valid"] is False
    assert result.plan["error_code"] == "DEPENDENCY_NOT_ACTIVE"
    assert await context.repository.global_version() == before
    document = await context.repository.get_module(
        "agentic_db_purchase"
    )
    assert document.workflow_state == ModuleState.SKIPPED
    assert document.draft is None


async def test_inactive_dependency_validation_does_not_stay_validating(
    context,
) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "agentic_db_purchase",
            expected_revision=0,
            config={},
        ),
        ADMIN,
    )

    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            command(
                ConfigAction.VALIDATE,
                "agentic_db_purchase",
                expected_revision=saved.module["revision"],
            ),
            ADMIN,
        )

    assert exc.value.code == "DEPENDENCY_NOT_ACTIVE"
    document = await context.repository.get_module(
        "agentic_db_purchase"
    )
    assert document.workflow_state == ModuleState.ERROR
    assert document.last_error_code == "DEPENDENCY_NOT_ACTIVE"
    assert document.validation_operation is None


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
            "agent_token_auth",
            expected_revision=0,
            config={"enabled": True},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    other = ConfigService(context.repository, context.crypto)
    activated = await other.execute(
        command(
            ConfigAction.ACTIVATE,
            "agent_token_auth",
            expected_revision=validated.module["revision"],
            validation_id=validated.validation["validation_id"],
            idempotency_key="activate-agent-token",
        ),
        ADMIN,
    )
    assert activated.module["workflow_state"] == ModuleState.ACTIVE
    assert activated.module["effective"]["config"] == {"enabled": True}


async def test_editing_validated_draft_invalidates_proof(context) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "agent_token_auth",
            expected_revision=0,
            config={"enabled": True},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    validation_id = validated.validation["validation_id"]
    edited = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "agent_token_auth",
            expected_revision=validated.module["revision"],
            config={"enabled": False},
        ),
        ADMIN,
    )
    with pytest.raises(ConfigError) as exc:
        await context.service.execute(
            command(
                ConfigAction.ACTIVATE,
                "agent_token_auth",
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
            "agent_token_auth",
            expected_revision=0,
            config={"enabled": True},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    document = await context.repository.get_module("agent_token_auth")
    document.last_validation.expires_at = datetime.now(
        timezone.utc
    ) - timedelta(seconds=1)
    await context.repository.compare_and_set_module(
        "agent_token_auth",
        expected_revision=validated.module["revision"],
        document=document,
    )

    described = await context.service.execute(
        command(ConfigAction.DESCRIBE, "agent_token_auth"),
        ADMIN,
    )
    assert described.module["workflow_state"] == ModuleState.DRAFT
    assert described.module["last_validation"] is None


async def test_expired_validation_lease_is_recovered(context) -> None:
    document = await context.repository.get_module("agent_token_auth")
    document.workflow_state = ModuleState.VALIDATING
    document.validation_operation = ValidationOperation(
        operation_id="abandoned",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=3),
        lease_expires_at=datetime.now(timezone.utc)
        - timedelta(minutes=1),
    )
    await context.repository.compare_and_set_module(
        "agent_token_auth",
        expected_revision=0,
        document=document,
    )

    recovered = await context.service.describe_internal(
        "agent_token_auth"
    )
    assert recovered.workflow_state == ModuleState.DRAFT
    assert recovered.last_error_code == "VALIDATION_INTERRUPTED"


async def test_validation_proof_replay_with_new_key_is_rejected(
    context,
) -> None:
    saved = await context.service.execute(
        command(
            ConfigAction.SAVE_DRAFT,
            "agent_token_auth",
            expected_revision=0,
            config={"enabled": True},
        ),
        ADMIN,
    )
    validated = await context.service.execute(
        command(
            ConfigAction.VALIDATE,
            "agent_token_auth",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )
    activation = command(
        ConfigAction.ACTIVATE,
        "agent_token_auth",
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
