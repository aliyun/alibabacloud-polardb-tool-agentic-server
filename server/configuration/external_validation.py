from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

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
from server.configuration.url_policy import is_strict_loopback_http_url
from server.logging import safe_request_id


_TRUSTED_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


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
    _oidc_max_response_bytes = 1_048_576

    def __init__(
        self,
        *,
        allow_insecure_loopback_urls: bool = False,
    ) -> None:
        self.allow_insecure_loopback_urls = allow_insecure_loopback_urls

    async def _require_safe_oidc_url(self, value: str) -> str:
        parsed = urlsplit(value)
        loopback_http = (
            self.allow_insecure_loopback_urls
            and is_strict_loopback_http_url(value)
        )
        if (
            (parsed.scheme != "https" and not loopback_http)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ExternalValidationError(
                "OIDC_ENDPOINT_UNSAFE",
                "OIDC endpoints must be public HTTPS URLs without credentials "
                "or fragments. Local development mode additionally permits "
                "exact localhost, 127.0.0.1, or ::1 HTTP URLs.",
            )
        try:
            addresses = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (80 if loopback_http else 443),
                    type=socket.SOCK_STREAM,
                ),
            )
        except socket.gaierror as exc:
            raise ExternalValidationError(
                "OIDC_DNS_FAILURE",
                "An OIDC endpoint could not be resolved by the server.",
            ) from exc
        if not addresses:
            raise ExternalValidationError(
                "OIDC_DNS_FAILURE",
                "An OIDC endpoint could not be resolved by the server.",
            )
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if loopback_http:
                if not ip.is_loopback:
                    raise ExternalValidationError(
                        "OIDC_ENDPOINT_UNSAFE",
                        "Local development OIDC endpoints must resolve only "
                        "to loopback addresses.",
                    )
            elif not ip.is_global:
                raise ExternalValidationError(
                    "OIDC_ENDPOINT_UNSAFE",
                    "OIDC endpoints must resolve only to public network addresses.",
                )
        return value

    def _oidc_network(self, value: str) -> str:
        if (
            self.allow_insecure_loopback_urls
            and is_strict_loopback_http_url(value)
        ):
            return "loopback"
        return "public"

    async def _require_safe_external_token_url(self, value: str) -> str:
        parsed = urlsplit(value)
        loopback_http = (
            self.allow_insecure_loopback_urls
            and is_strict_loopback_http_url(value)
        )
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ExternalValidationError(
                "OIDC_ENDPOINT_UNSAFE",
                "External token endpoints must be HTTP or HTTPS URLs without "
                "credentials or fragments.",
            )
        try:
            addresses = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (80 if parsed.scheme == "http" else 443),
                    type=socket.SOCK_STREAM,
                ),
            )
        except socket.gaierror as exc:
            raise ExternalValidationError(
                "OIDC_DNS_FAILURE",
                "An external token endpoint could not be resolved by the server.",
            ) from exc
        if not addresses:
            raise ExternalValidationError(
                "OIDC_DNS_FAILURE",
                "An external token endpoint could not be resolved by the server.",
            )

        networks: set[str] = set()
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_loopback:
                if not loopback_http:
                    raise ExternalValidationError(
                        "OIDC_ENDPOINT_UNSAFE",
                        "Loopback external token endpoints require localhost "
                        "development mode.",
                    )
                networks.add("loopback")
            elif (
                ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
                or ip.is_reserved
            ):
                raise ExternalValidationError(
                    "OIDC_ENDPOINT_UNSAFE",
                    "External token endpoints cannot resolve to link-local, "
                    "multicast, unspecified, or reserved addresses.",
                )
            elif any(ip in network for network in _TRUSTED_PRIVATE_NETWORKS):
                networks.add("private")
            elif ip.is_global:
                networks.add("public")
            else:
                raise ExternalValidationError(
                    "OIDC_ENDPOINT_UNSAFE",
                    "External token endpoints must resolve only to public or "
                    "approved private network addresses.",
                )

        if len(networks) != 1:
            raise ExternalValidationError(
                "OIDC_ENDPOINT_UNSAFE",
                "External token endpoints cannot mix public, private, and "
                "loopback address classes.",
            )
        if parsed.scheme == "http" and networks == {"public"}:
            raise ExternalValidationError(
                "OIDC_ENDPOINT_UNSAFE",
                "HTTP external token endpoints must resolve only to trusted "
                "private network addresses. Use HTTPS for public endpoints.",
            )
        return value

    async def _get_oidc_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        label: str,
    ) -> dict[str, Any]:
        await self._require_safe_oidc_url(url)
        try:
            response = await client.get(url)
            if 300 <= response.status_code < 400:
                raise ExternalValidationError(
                    "OIDC_REDIRECT_REJECTED",
                    f"The OIDC {label} endpoint returned a redirect.",
                )
            response.raise_for_status()
        except ExternalValidationError:
            raise
        except httpx.TimeoutException as exc:
            raise ExternalValidationError(
                "OIDC_CONNECT_TIMEOUT",
                f"The OIDC {label} endpoint timed out.",
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalValidationError(
                "OIDC_CONNECT_FAILURE",
                f"The OIDC {label} endpoint could not be reached.",
            ) from exc
        if len(response.content) > self._oidc_max_response_bytes:
            raise ExternalValidationError(
                "OIDC_RESPONSE_TOO_LARGE",
                f"The OIDC {label} response exceeds 1 MiB.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalValidationError(
                "OIDC_INVALID_RESPONSE",
                f"The OIDC {label} response must be valid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalValidationError(
                "OIDC_INVALID_RESPONSE",
                f"The OIDC {label} response must be a JSON object.",
            )
        return payload

    async def _validate_oidc(
        self,
        config: Mapping[str, Any],
    ) -> ExternalValidationResult:
        checks: list[ExternalValidationCheck] = []
        browser_login_enabled = config.get("browser_login_enabled", True) is True
        discovery_url = config.get("discovery_url")
        metadata: Mapping[str, Any] = {}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        ) as client:
            if (
                browser_login_enabled
                and isinstance(discovery_url, str)
                and discovery_url
            ):
                metadata = await self._get_oidc_json(
                    client,
                    discovery_url,
                    label="discovery",
                )
                checks.append(
                    ExternalValidationCheck(
                        service="oidc_discovery",
                        network=self._oidc_network(discovery_url),
                        endpoint=discovery_url,
                        status="REACHABLE",
                    )
                )

            def endpoint(name: str) -> str | None:
                configured = config.get(name)
                discovered = metadata.get(name)
                value = configured or discovered
                return value if isinstance(value, str) and value else None

            issuer = endpoint("issuer")
            if browser_login_enabled and not issuer:
                raise ExternalValidationError(
                    "OIDC_ISSUER_REQUIRED",
                    "OIDC discovery metadata must provide an issuer.",
                )
            configured_issuer = config.get("issuer")
            discovered_issuer = metadata.get("issuer")
            if (
                configured_issuer
                and discovered_issuer
                and configured_issuer != discovered_issuer
            ):
                raise ExternalValidationError(
                    "OIDC_ISSUER_MISMATCH",
                    "The configured issuer does not match discovery metadata.",
                )

            if browser_login_enabled:
                for name in (
                    "issuer",
                    "authorization_endpoint",
                    "token_endpoint",
                ):
                    value = endpoint(name)
                    if not value:
                        raise ExternalValidationError(
                            "OIDC_ENDPOINT_REQUIRED",
                            f"OIDC configuration requires {name}.",
                        )
                    await self._require_safe_oidc_url(value)

            userinfo_endpoint = endpoint("userinfo_endpoint")
            if browser_login_enabled and userinfo_endpoint:
                await self._require_safe_oidc_url(userinfo_endpoint)
            protocol_mode = str(config.get("protocol_mode") or "oidc")
            if (
                browser_login_enabled
                and protocol_mode == "oauth2_userinfo"
                and not userinfo_endpoint
            ):
                raise ExternalValidationError(
                    "OAUTH_USERINFO_REQUIRED",
                    "OAuth 2.0 UserInfo mode requires a UserInfo endpoint.",
                )
            trust = config.get("external_token_trust")
            trust_config = trust if isinstance(trust, Mapping) else {}
            introspection_endpoint = trust_config.get(
                "introspection_endpoint"
            )
            if (
                trust_config.get("enabled") is True
                and trust_config.get("provider")
                == "oauth2_introspection"
                and isinstance(introspection_endpoint, str)
            ):
                await self._require_safe_external_token_url(
                    introspection_endpoint
                )
            external_userinfo_endpoint = trust_config.get(
                "userinfo_endpoint"
            )
            if (
                trust_config.get("enabled") is True
                and isinstance(external_userinfo_endpoint, str)
            ):
                await self._require_safe_external_token_url(
                    external_userinfo_endpoint
                )
            jwks_uri = endpoint("jwks_uri")
            jwks_required = browser_login_enabled and (
                protocol_mode == "oidc"
                or (
                    trust_config.get("enabled") is True
                    and trust_config.get("provider") == "oidc_jwt"
                )
            )
            if jwks_required and not jwks_uri:
                raise ExternalValidationError(
                    "OIDC_JWKS_REQUIRED",
                    "OIDC login or JWT token trust requires a JWKS URI.",
                )
            if jwks_required and jwks_uri is not None:
                jwks = await self._get_oidc_json(
                    client,
                    jwks_uri,
                    label="JWKS",
                )
                keys = jwks.get("keys")
                if (
                    not isinstance(keys, list)
                    or not keys
                    or any(not isinstance(key, dict) for key in keys)
                ):
                    raise ExternalValidationError(
                        "OIDC_INVALID_JWKS",
                        "The OIDC JWKS response must contain a non-empty keys array.",
                    )
                checks.append(
                    ExternalValidationCheck(
                        service="oidc_jwks",
                        network=self._oidc_network(jwks_uri),
                        endpoint=jwks_uri,
                        status="REACHABLE",
                    )
                )
        return ExternalValidationResult(
            status="PASSED",
            checks=tuple(checks),
        )

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
        if module == "user_sso":
            return await self._validate_oidc(config)
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
