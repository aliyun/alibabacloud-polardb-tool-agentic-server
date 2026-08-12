from __future__ import annotations

import socket
import ssl
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from server.aliyun.credential_provider import (
    AssumeRoleProvider,
    CredentialProvider,
    DirectAKProvider,
    ECSRamRoleProvider,
)
from server.aliyun.diagnostics import request_id_from_error
from server.aliyun.ecs_metadata import (
    ECSMetadataError,
    resolve_ecs_role_name_v2,
)
from server.aliyun.endpoints import (
    OpenAPIEndpointError,
    resolve_openapi_endpoint,
)
from server.aliyun.polardb_client_impl import AliyunPolarDBClient
from server.configuration.secrets import mask_access_key_id
from server.logging import safe_request_id


@dataclass(frozen=True, slots=True)
class ExternalValidationCheck:
    service: str
    network: str
    endpoint: str
    status: str
    identity_hint: str | None = None
    expires_at: int | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalValidationResult:
    status: str
    checks: tuple[ExternalValidationCheck, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [
                {
                    field: value
                    for field, value in asdict(check).items()
                    if value is not None
                }
                for check in self.checks
            ],
        }


class ExternalValidationError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, request_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = safe_request_id(request_id)


class ExternalModuleValidator(Protocol):
    async def validate(
        self,
        module: str,
        config: Mapping[str, Any],
    ) -> ExternalValidationResult: ...


class NoopExternalModuleValidator:
    async def validate(
        self,
        module: str,
        config: Mapping[str, Any],
    ) -> ExternalValidationResult:
        del module, config
        return ExternalValidationResult(status="SKIPPED")


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


OpenAPIValidationStage = Literal["assume_role", "ecs_metadata", "polardb"]


def _safe_request_id(error: BaseException) -> str | None:
    return request_id_from_error(error)


def _error(code: str, message: str, error: BaseException) -> ExternalValidationError:
    return ExternalValidationError(
        code, message, request_id=_safe_request_id(error)
    )


def map_openapi_error(
    error: BaseException, stage: OpenAPIValidationStage
) -> ExternalValidationError:
    chain = _exception_chain(error)
    if isinstance(error, OpenAPIEndpointError):
        return _error(
            "OPENAPI_ENDPOINT_UNSUPPORTED",
            "The selected service, region, or network has no supported "
            "OpenAPI endpoint.",
            error,
        )
    if stage == "ecs_metadata":
        reason = error.reason if isinstance(error, ECSMetadataError) else ""
        code = str(getattr(error, "code", "")).lower()
        if reason == "role_not_attached" or "rolenotfound" in code:
            return _error(
                "OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED",
                "No ECS RAM role is attached to this instance.",
                error,
            )
        if reason == "metadata_disabled" or any(
            marker in code for marker in ("metadata", "forbidden", "disabled")
        ):
            return _error(
                "OPENAPI_ECS_METADATA_DISABLED",
                "ECS instance metadata access is disabled for this instance.",
                error,
            )
        return _error(
            "OPENAPI_ECS_IMDSV2_UNAVAILABLE",
            "The server could not obtain an ECS RAM role through IMDSv2.",
            error,
        )
    if any(isinstance(item, socket.gaierror) for item in chain):
        return _error(
            "OPENAPI_DNS_FAILURE",
            "The configured OpenAPI endpoint could not be resolved "
            "by the server.",
            error,
        )
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return _error(
            "OPENAPI_TLS_FAILURE",
            "The server could not establish a trusted TLS connection "
            "to the OpenAPI endpoint.",
            error,
        )

    code = str(getattr(error, "code", "")).lower()
    if "expired" in code:
        return _error(
            "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED",
            "The temporary Alibaba Cloud credential has expired.",
            error,
        )
    if stage == "assume_role":
        if "externalid" in code:
            return _error(
                "OPENAPI_STS_EXTERNAL_ID_MISMATCH",
                "Alibaba Cloud rejected the AssumeRole external ID.",
                error,
            )
        if any(marker in code for marker in ("trust", "assumeroleforbidden")):
            return _error(
                "OPENAPI_STS_ROLE_TRUST_REJECTED",
                "The target role trust policy rejected the AssumeRole request.",
                error,
            )
        if any(
            marker in code
            for marker in (
                "invalidaccesskey",
                "signaturedoesnotmatch",
                "invalidsecuritytoken",
                "missingsecuritytoken",
            )
        ):
            return _error(
                "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID",
                "Alibaba Cloud rejected the configured AssumeRole source credential.",
                error,
            )
        if any(
            marker in code
            for marker in ("forbidden", "unauthorized", "permission", "denied")
        ):
            return _error(
                "OPENAPI_STS_ASSUME_ROLE_DENIED",
                "The source credential is not permitted to assume the target role.",
                error,
            )
    if any(
        marker in code
        for marker in (
            "invalidaccesskey",
            "signaturedoesnotmatch",
            "invalidsecuritytoken",
            "missingsecuritytoken",
        )
    ):
        return _error(
            "OPENAPI_CREDENTIAL_INVALID",
            "Alibaba Cloud rejected the configured credential.",
            error,
        )
    if any(
        marker in code
        for marker in ("forbidden", "unauthorized", "permission", "denied")
    ):
        return _error(
            "OPENAPI_PERMISSION_DENIED",
            "The configured credential is not permitted to read "
            "PolarDB metadata.",
            error,
        )
    if any(
        isinstance(item, (ConnectionError, TimeoutError, OSError))
        for item in chain
    ):
        return _error(
            "OPENAPI_CONNECT_FAILURE",
            "The server could not connect to the configured OpenAPI "
            "endpoint.",
            error,
        )
    return _error(
        "OPENAPI_CONNECT_FAILURE",
        "The backend could not complete the OpenAPI connectivity check.",
        error,
    )


def _external_error(error: BaseException) -> ExternalValidationError:
    """Compatibility mapping for existing PolarDB validation callers."""
    return map_openapi_error(error, "polardb")


def _identity_hint(role_name: str | None) -> str | None:
    if not role_name:
        return None
    name = role_name.rsplit("/", maxsplit=1)[-1]
    if len(name) <= 2:
        return "**"
    return f"{name[:2]}***"


class AlibabaCloudExternalValidator:
    def build_direct_provider(
        self, config: Mapping[str, Any]
    ) -> CredentialProvider:
        credentials = config["direct_ak"]
        assert isinstance(credentials, Mapping)
        return DirectAKProvider(
            access_key_id=str(credentials["access_key_id"]),
            access_key_secret=str(credentials["access_key_secret"]),
            region_id=str(config["region_id"]),
            openapi_network=str(config["openapi_network"]),
        )

    def build_assume_provider(
        self, config: Mapping[str, Any]
    ) -> CredentialProvider:
        credentials = config["assume_role"]
        assert isinstance(credentials, Mapping)
        return AssumeRoleProvider(
            source_access_key_id=str(credentials["source_access_key_id"]),
            source_access_key_secret=str(credentials["source_access_key_secret"]),
            role_arn=str(credentials["role_arn"]),
            role_session_name=str(credentials["role_session_name"]),
            duration_seconds=int(credentials["duration_seconds"]),
            external_id=(
                str(credentials["external_id"])
                if credentials.get("external_id") is not None
                else None
            ),
            region_id=str(config["region_id"]),
            openapi_network=str(config["openapi_network"]),
        )

    def build_ecs_provider(
        self, config: Mapping[str, Any], role_name: str
    ) -> CredentialProvider:
        return ECSRamRoleProvider(
            role_name=role_name,
            region_id=str(config["region_id"]),
            openapi_network=str(config["openapi_network"]),
        )

    async def validate(
        self,
        module: str,
        config: Mapping[str, Any],
    ) -> ExternalValidationResult:
        if module != "aliyun_access":
            return ExternalValidationResult(status="SKIPPED")

        region = str(config["region_id"])
        network = str(config["openapi_network"])
        mode = str(config["credential_mode"])
        checks: list[ExternalValidationCheck] = []
        direct_identity_hint: str | None = None
        provider: CredentialProvider
        stage: OpenAPIValidationStage = "polardb"
        try:
            polardb_endpoint = resolve_openapi_endpoint("polardb", region, network)
            if mode == "direct_ak":
                direct_credentials = config["direct_ak"]
                assert isinstance(direct_credentials, Mapping)
                direct_identity_hint = mask_access_key_id(
                    str(direct_credentials["access_key_id"])
                )
                provider = self.build_direct_provider(config)
            elif mode == "assume_role":
                stage = "assume_role"
                sts_endpoint = resolve_openapi_endpoint(
                    "sts", region, network
                )
                provider = self.build_assume_provider(config)
                await provider.get_credentials()
                stage = "polardb"
                probe = provider.probe()
                checks.append(
                    ExternalValidationCheck(
                        service="sts",
                        network=network,
                        endpoint=sts_endpoint,
                        status="REACHABLE",
                        identity_hint=_identity_hint(
                            probe.role_name
                            or str(config["assume_role"]["role_arn"])
                        ),
                        expires_at=probe.expires_at,
                    )
                )
            else:
                stage = "ecs_metadata"
                role_name = await resolve_ecs_role_name_v2()
                checks.append(
                    ExternalValidationCheck(
                        service="ecs_metadata",
                        network=network,
                        endpoint="http://100.100.100.200",
                        status="REACHABLE",
                        identity_hint=_identity_hint(role_name),
                    )
                )
                provider = self.build_ecs_provider(config, role_name)
                await provider.get_credentials()
                stage = "polardb"

            polardb_client = AliyunPolarDBClient(provider)
            await polardb_client.discover_clusters(region)
            checks.append(
                ExternalValidationCheck(
                    service="polardb",
                    network=network,
                    endpoint=polardb_endpoint,
                    status="REACHABLE",
                    identity_hint=direct_identity_hint,
                    request_id=safe_request_id(
                        getattr(polardb_client, "last_request_id", None)
                    ),
                )
            )
            return ExternalValidationResult(
                status="PASSED",
                checks=tuple(checks),
            )
        except ExternalValidationError:
            raise
        except Exception as error:
            raise map_openapi_error(error, stage) from None
