from __future__ import annotations

import socket
import ssl
from unittest.mock import AsyncMock

import pytest

from server.configuration.external_validation import (
    ExternalValidationCheck,
    ExternalValidationError,
    ExternalValidationResult,
    _external_error,
)
from server.aliyun.endpoints import OpenAPIEndpointError
from server.configuration.service import ConfigService
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
    ConfigError,
    ModuleState,
)
from tests._configuration_helpers import create_config_context

ADMIN = ConfigActor(scope="admin:1", actor_type="admin")
ALIYUN_CONFIG = {
    "credential_mode": "direct_ak",
    "access_key_id": "test-ak",
    "access_key_secret": "test-secret",
    "region_id": "cn-beijing",
    "openapi_network": "vpc",
}


@pytest.fixture
async def context():
    value = await create_config_context()
    yield value
    await value.close()


def _validator_result() -> ExternalValidationResult:
    return ExternalValidationResult(
        status="PASSED",
        checks=(
            ExternalValidationCheck(
                service="polardb",
                network="vpc",
                endpoint="polardb-vpc.cn-beijing.aliyuncs.com",
                status="REACHABLE",
            ),
        ),
    )


class _CodedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__("sensitive upstream detail")
        self.code = code


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (socket.gaierror(), "OPENAPI_DNS_FAILURE"),
        (ssl.SSLError(), "OPENAPI_TLS_FAILURE"),
        (ConnectionError(), "OPENAPI_CONNECT_FAILURE"),
        (
            OpenAPIEndpointError("unsupported"),
            "OPENAPI_ENDPOINT_UNSUPPORTED",
        ),
        (
            _CodedError("InvalidAccessKeyId.NotFound"),
            "OPENAPI_CREDENTIAL_INVALID",
        ),
        (
            _CodedError("Forbidden.RAM"),
            "OPENAPI_PERMISSION_DENIED",
        ),
    ],
)
def test_external_errors_are_mapped_to_sanitized_categories(
    error: Exception,
    expected_code: str,
) -> None:
    mapped = _external_error(error)

    assert mapped.code == expected_code
    assert "sensitive upstream detail" not in mapped.message


async def test_aliyun_plan_runs_external_validation_without_writes(
    context,
) -> None:
    validator = AsyncMock()
    validator.validate.return_value = _validator_result()
    service = ConfigService(
        context.repository,
        context.crypto,
        external_validator=validator,
    )
    before = await context.repository.global_version()

    result = await service.execute(
        ConfigCommand(
            action=ConfigAction.PLAN,
            module="aliyun_access",
            config=ALIYUN_CONFIG,
        ),
        ADMIN,
    )

    normalized = {
        **ALIYUN_CONFIG,
        "role_arn": "",
        "role_session_name": "polardb-agentic",
        "sts_duration_seconds": 3600,
    }
    validator.validate.assert_awaited_once_with(
        "aliyun_access",
        normalized,
    )
    assert result.plan["valid"] is True
    assert result.plan["external_validation"] == {
        "status": "PASSED",
        "checks": [
            {
                "service": "polardb",
                "network": "vpc",
                "endpoint": "polardb-vpc.cn-beijing.aliyuncs.com",
                "status": "REACHABLE",
            }
        ],
    }
    assert await context.repository.global_version() == before


async def test_schema_failure_prevents_external_validation(context) -> None:
    validator = AsyncMock()
    service = ConfigService(
        context.repository,
        context.crypto,
        external_validator=validator,
    )

    result = await service.execute(
        ConfigCommand(
            action=ConfigAction.PLAN,
            module="aliyun_access",
            config={"openapi_network": "custom"},
        ),
        ADMIN,
    )

    assert result.plan["valid"] is False
    validator.validate.assert_not_awaited()


async def test_other_modules_do_not_perform_openapi_io(context) -> None:
    validator = AsyncMock()
    service = ConfigService(
        context.repository,
        context.crypto,
        external_validator=validator,
    )

    await service.execute(
        ConfigCommand(
            action=ConfigAction.PLAN,
            module="agent_token_auth",
            config={"enabled": True},
        ),
        ADMIN,
    )

    validator.validate.assert_not_awaited()


async def test_validate_failure_is_sanitized_and_transitions_to_error(
    context,
) -> None:
    validator = AsyncMock()
    validator.validate.side_effect = ExternalValidationError(
        "OPENAPI_DNS_FAILURE",
        "The configured OpenAPI endpoint could not be resolved by the server.",
    )
    service = ConfigService(
        context.repository,
        context.crypto,
        external_validator=validator,
    )
    saved = await service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=0,
            config=ALIYUN_CONFIG,
        ),
        ADMIN,
    )

    with pytest.raises(ConfigError) as error:
        await service.execute(
            ConfigCommand(
                action=ConfigAction.VALIDATE,
                module="aliyun_access",
                expected_revision=saved.module["revision"],
            ),
            ADMIN,
        )

    assert error.value.code == "OPENAPI_DNS_FAILURE"
    assert "test-ak" not in str(error.value)
    assert "test-secret" not in str(error.value)
    document = await context.repository.get_module("aliyun_access")
    assert document.workflow_state == ModuleState.ERROR
    assert document.last_error_code == "OPENAPI_DNS_FAILURE"
    assert "test-secret" not in document.model_dump_json()


async def test_validate_returns_sanitized_external_result(context) -> None:
    validator = AsyncMock()
    validator.validate.return_value = _validator_result()
    service = ConfigService(
        context.repository,
        context.crypto,
        external_validator=validator,
    )
    saved = await service.execute(
        ConfigCommand(
            action=ConfigAction.SAVE_DRAFT,
            module="aliyun_access",
            expected_revision=0,
            config=ALIYUN_CONFIG,
        ),
        ADMIN,
    )

    result = await service.execute(
        ConfigCommand(
            action=ConfigAction.VALIDATE,
            module="aliyun_access",
            expected_revision=saved.module["revision"],
        ),
        ADMIN,
    )

    assert result.validation["external_validation"]["status"] == "PASSED"
    assert "test-ak" not in result.model_dump_json()
    assert "test-secret" not in result.model_dump_json()
