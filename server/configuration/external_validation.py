from __future__ import annotations

import socket
import ssl
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from server.aliyun.credential_provider import (
    AssumeRoleProvider,
    CredentialProvider,
    DirectAKProvider,
)
from server.aliyun.endpoints import (
    OpenAPIEndpointError,
    resolve_openapi_endpoint,
)
from server.aliyun.polardb_client_impl import AliyunPolarDBClient


@dataclass(frozen=True, slots=True)
class ExternalValidationCheck:
    service: str
    network: str
    endpoint: str
    status: str


@dataclass(frozen=True, slots=True)
class ExternalValidationResult:
    status: str
    checks: tuple[ExternalValidationCheck, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [asdict(check) for check in self.checks],
        }


class ExternalValidationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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


def _external_error(error: BaseException) -> ExternalValidationError:
    chain = _exception_chain(error)
    if any(isinstance(item, socket.gaierror) for item in chain):
        return ExternalValidationError(
            "OPENAPI_DNS_FAILURE",
            "The configured OpenAPI endpoint could not be resolved "
            "by the server.",
        )
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return ExternalValidationError(
            "OPENAPI_TLS_FAILURE",
            "The server could not establish a trusted TLS connection "
            "to the OpenAPI endpoint.",
        )

    code = str(getattr(error, "code", "")).lower()
    if any(
        marker in code
        for marker in (
            "invalidaccesskey",
            "signaturedoesnotmatch",
            "invalidsecuritytoken",
            "missingsecuritytoken",
        )
    ):
        return ExternalValidationError(
            "OPENAPI_CREDENTIAL_INVALID",
            "Alibaba Cloud rejected the configured credential.",
        )
    if any(
        marker in code
        for marker in ("forbidden", "unauthorized", "permission", "denied")
    ):
        return ExternalValidationError(
            "OPENAPI_PERMISSION_DENIED",
            "The configured credential is not permitted to read "
            "PolarDB metadata.",
        )
    if any(
        isinstance(item, (ConnectionError, TimeoutError, OSError))
        for item in chain
    ):
        return ExternalValidationError(
            "OPENAPI_CONNECT_FAILURE",
            "The server could not connect to the configured OpenAPI "
            "endpoint.",
        )
    if isinstance(error, OpenAPIEndpointError):
        return ExternalValidationError(
            "OPENAPI_ENDPOINT_UNSUPPORTED",
            "The selected service, region, or network has no supported "
            "OpenAPI endpoint.",
        )
    return ExternalValidationError(
        "OPENAPI_CONNECT_FAILURE",
        "The backend could not complete the OpenAPI connectivity check.",
    )


class AlibabaCloudExternalValidator:
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
        try:
            polardb_endpoint = resolve_openapi_endpoint(
                "polardb", region, network
            )
            checks: list[ExternalValidationCheck] = []
            provider: CredentialProvider
            if mode == "assume_role":
                sts_endpoint = resolve_openapi_endpoint(
                    "sts", region, network
                )
                provider = AssumeRoleProvider(
                    ak=str(config["access_key_id"]),
                    sk=str(config["access_key_secret"]),
                    role_arn=str(config["role_arn"]),
                    session_name=str(config["role_session_name"]),
                    duration=int(config["sts_duration_seconds"]),
                    region_id=region,
                    openapi_network=network,
                )
                await provider.get_credentials()
                checks.append(
                    ExternalValidationCheck(
                        service="sts",
                        network=network,
                        endpoint=sts_endpoint,
                        status="REACHABLE",
                    )
                )
            else:
                provider = DirectAKProvider(
                    ak=str(config["access_key_id"]),
                    sk=str(config["access_key_secret"]),
                    region_id=region,
                    openapi_network=network,
                )

            await AliyunPolarDBClient(provider).discover_clusters(region)
            checks.append(
                ExternalValidationCheck(
                    service="polardb",
                    network=network,
                    endpoint=polardb_endpoint,
                    status="REACHABLE",
                )
            )
            return ExternalValidationResult(
                status="PASSED",
                checks=tuple(checks),
            )
        except ExternalValidationError:
            raise
        except Exception as error:
            raise _external_error(error) from None
