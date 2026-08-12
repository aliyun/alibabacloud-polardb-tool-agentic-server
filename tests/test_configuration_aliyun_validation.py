from __future__ import annotations

import socket
import ssl
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.configuration.external_validation import (
    AlibabaCloudExternalValidator,
    ExternalValidationCheck,
    ExternalValidationError,
    ExternalValidationResult,
    _external_error,
    map_openapi_error,
)
from server.aliyun.credential_provider import AliyunCredentialProvider
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
    "direct_ak": {
        "access_key_id": "test-ak",
        "access_key_secret": "test-secret",
    },
    "region_id": "cn-beijing",
    "openapi_network": "vpc",
}
ASSUME_ROLE_CONFIG = {
    "credential_mode": "assume_role",
    "assume_role": {
        "source_access_key_id": "test-source-ak",
        "source_access_key_secret": "test-source-secret",
        "role_arn": "acs:ram::123456789012:role/pas-runtime",
        "role_session_name": "pas-validation",
        "duration_seconds": 3600,
        "external_id": "sensitive-external-id",
    },
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
    def __init__(self, code: str, request_id: str | None = None) -> None:
        super().__init__("sensitive upstream detail")
        self.code = code
        self.request_id = request_id


class _FailingRawProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_provider_name(self) -> str:
        return "failing-provider"

    def get_credentials(self):
        raise self.error

    async def get_credentials_async(self):
        raise self.error


@pytest.mark.parametrize(
    ("stage", "sdk_code", "expected"),
    [
        (
            "assume_role",
            "InvalidAccessKeyId.NotFound",
            "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID",
        ),
        (
            "assume_role",
            "NoPermission",
            "OPENAPI_STS_ASSUME_ROLE_DENIED",
        ),
        (
            "ecs_metadata",
            "RoleNotFound",
            "OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED",
        ),
        (
            "polardb",
            "Forbidden.RAM",
            "OPENAPI_PERMISSION_DENIED",
        ),
        (
            "polardb",
            "InvalidSecurityToken",
            "OPENAPI_CREDENTIAL_INVALID",
        ),
        (
            "assume_role",
            "InvalidSecurityToken",
            "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID",
        ),
        (
            "polardb",
            "InvalidSecurityToken.Expired",
            "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED",
        ),
    ],
)
def test_stage_aware_error_mapping(
    stage: str, sdk_code: str, expected: str
) -> None:
    assert map_openapi_error(_CodedError(sdk_code), stage).code == expected


async def test_direct_dry_run_never_calls_sts(monkeypatch) -> None:
    validator = AlibabaCloudExternalValidator()
    sts = AsyncMock(
        side_effect=AssertionError("direct mode must not call STS")
    )
    provider = AsyncMock()
    provider.probe.return_value = None
    monkeypatch.setattr(validator, "build_assume_provider", sts)
    monkeypatch.setattr(validator, "build_direct_provider", lambda _: provider)
    monkeypatch.setattr(
        "server.configuration.external_validation.AliyunPolarDBClient",
        lambda _: AsyncMock(discover_clusters=AsyncMock(return_value=[])),
    )

    await validator.validate("aliyun_access", ALIYUN_CONFIG)

    sts.assert_not_awaited()


@pytest.mark.parametrize(
    ("sdk_code", "expected"),
    [
        ("InvalidAccessKeyId.NotFound", "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID"),
        ("NoPermission", "OPENAPI_STS_ASSUME_ROLE_DENIED"),
        ("AssumeRoleForbidden", "OPENAPI_STS_ROLE_TRUST_REJECTED"),
        ("ExternalIdMismatch", "OPENAPI_STS_EXTERNAL_ID_MISMATCH"),
    ],
)
async def test_assume_role_provider_failures_keep_stage_specific_codes(
    monkeypatch, sdk_code: str, expected: str
) -> None:
    validator = AlibabaCloudExternalValidator()
    provider = AliyunCredentialProvider(
        _FailingRawProvider(
            _CodedError(sdk_code, request_id="sts-request:123")
        ),
        mode="assume_role",
        region_id="cn-beijing",
        openapi_network="vpc",
    )
    monkeypatch.setattr(validator, "build_assume_provider", lambda _: provider)

    with pytest.raises(ExternalValidationError) as error:
        await validator.validate("aliyun_access", ASSUME_ROLE_CONFIG)

    assert error.value.code == expected
    assert error.value.request_id == "sts-request:123"
    assert "sensitive upstream detail" not in error.value.message
    assert "sensitive-external-id" not in error.value.message


@pytest.mark.parametrize(
    ("request_id", "expected"),
    [
        ("safe-request:123", "safe-request:123"),
        ("\u79d8\u5bc6-request", None),
        (None, None),
    ],
)
def test_request_id_mapping_accepts_only_repository_safe_ids(
    request_id: str | None, expected: str | None
) -> None:
    error = _CodedError("Forbidden.RAM", request_id=request_id)
    error.response_body = "requestId=body-id access_key_secret=not-safe"

    mapped = map_openapi_error(error, "polardb")

    assert mapped.request_id == expected
    assert "not-safe" not in mapped.message


async def test_successful_polardb_check_includes_safe_request_id(monkeypatch) -> None:
    validator = AlibabaCloudExternalValidator()
    provider = AsyncMock()
    client = AsyncMock()
    client.last_request_id = "polardb-request:456"
    client.discover_clusters.return_value = []
    monkeypatch.setattr(validator, "build_direct_provider", lambda _: provider)
    monkeypatch.setattr(
        "server.configuration.external_validation.AliyunPolarDBClient",
        lambda _: client,
    )

    result = await validator.validate("aliyun_access", ALIYUN_CONFIG)

    assert result.as_dict()["checks"][-1]["request_id"] == "polardb-request:456"


async def test_direct_dry_run_returns_only_the_server_masked_access_key_id(
    monkeypatch,
) -> None:
    validator = AlibabaCloudExternalValidator()
    provider = AsyncMock()
    client = AsyncMock()
    client.last_request_id = "polardb-request:masked"
    client.discover_clusters.return_value = []
    monkeypatch.setattr(validator, "build_direct_provider", lambda _: provider)
    monkeypatch.setattr(
        "server.configuration.external_validation.AliyunPolarDBClient",
        lambda _: client,
    )
    config = {
        **ALIYUN_CONFIG,
        "direct_ak": {
            "access_key_id": "TEST1234567890ABCD",
            "access_key_secret": "direct-secret-must-not-return",
        },
    }

    result = await validator.validate("aliyun_access", config)
    payload = json.dumps(result.as_dict())

    assert result.as_dict()["checks"][-1]["identity_hint"] == "TEST****ABCD"
    assert "TEST1234567890ABCD" not in payload
    assert "direct-secret-must-not-return" not in payload


async def test_direct_dry_run_failure_does_not_copy_identity_from_sdk_body(
    monkeypatch,
) -> None:
    validator = AlibabaCloudExternalValidator()
    provider = AsyncMock()
    upstream = _CodedError(
        "Forbidden.RAM",
        request_id="polardb-request:failure",
    )
    upstream.response_body = (
        "AccessKeyId=TEST1234567890ABCD "
        "AccessKeySecret=direct-secret-must-not-return"
    )
    client = AsyncMock()
    client.discover_clusters.side_effect = upstream
    monkeypatch.setattr(validator, "build_direct_provider", lambda _: provider)
    monkeypatch.setattr(
        "server.configuration.external_validation.AliyunPolarDBClient",
        lambda _: client,
    )

    with pytest.raises(ExternalValidationError) as error:
        await validator.validate("aliyun_access", ALIYUN_CONFIG)

    payload = json.dumps(error.value.__dict__, default=str)
    assert error.value.request_id == "polardb-request:failure"
    assert "identity" not in payload
    assert "TEST1234567890ABCD" not in payload
    assert "direct-secret-must-not-return" not in payload


@pytest.mark.parametrize(
    ("sdk_code", "expected"),
    [
        ("InvalidAccessKeyId.NotFound", "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID"),
        ("NoPermission", "OPENAPI_STS_ASSUME_ROLE_DENIED"),
        ("AssumeRoleForbidden", "OPENAPI_STS_ROLE_TRUST_REJECTED"),
        ("ExternalIdMismatch", "OPENAPI_STS_EXTERNAL_ID_MISMATCH"),
    ],
)
async def test_constructed_assume_role_sanitizes_real_sts_failures(
    monkeypatch, caplog, sdk_code: str, expected: str
) -> None:
    response = SimpleNamespace(
        status_code=403,
        body=json.dumps(
            {
                "Code": sdk_code,
                "RequestId": "sts-real-request:123",
                "AccessKeySecret": "upstream-secret",
                "ExternalId": "upstream-external-id",
            }
        ).encode(),
    )

    async def async_do_action(*args, **kwargs):
        del args, kwargs
        return response

    monkeypatch.setattr(
        "server.aliyun.safe_ram_role.TeaCore.async_do_action",
        async_do_action,
    )
    validator = AlibabaCloudExternalValidator()

    with pytest.raises(ExternalValidationError) as error:
        await validator.validate("aliyun_access", ASSUME_ROLE_CONFIG)

    assert error.value.code == expected
    assert error.value.request_id == "sts-real-request:123"
    assert "upstream-secret" not in caplog.text
    assert "upstream-external-id" not in caplog.text


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
        "assume_role": None,
        "ecs_ram_role": None,
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


async def test_aliyun_plan_serializes_masked_direct_identity_only(
    context,
) -> None:
    validator = AsyncMock()
    validator.validate.return_value = ExternalValidationResult(
        status="PASSED",
        checks=(
            ExternalValidationCheck(
                service="polardb",
                network="public",
                endpoint="polardb.aliyuncs.com",
                status="REACHABLE",
                identity_hint="TEST****ABCD",
            ),
        ),
    )
    service = ConfigService(
        context.repository,
        context.crypto,
        external_validator=validator,
    )

    result = await service.execute(
        ConfigCommand(
            action=ConfigAction.PLAN,
            module="aliyun_access",
            config={
                **ALIYUN_CONFIG,
                "direct_ak": {
                    "access_key_id": "TEST1234567890ABCD",
                    "access_key_secret": "direct-secret-must-not-return",
                },
            },
        ),
        ADMIN,
    )
    payload = json.dumps(result.plan)

    assert result.plan["external_validation"]["checks"][-1][
        "identity_hint"
    ] == "TEST****ABCD"
    assert "TEST1234567890ABCD" not in payload
    assert "direct-secret-must-not-return" not in payload


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
            module="runtime_policy",
            config={},
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
