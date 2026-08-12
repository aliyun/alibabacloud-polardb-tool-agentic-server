from __future__ import annotations

from dataclasses import dataclass

from alibabacloud_credentials.client import Client as CredentialsClient
from alibabacloud_credentials.http import HttpOptions
from alibabacloud_credentials.provider import (
    EcsRamRoleCredentialsProvider,
    StaticAKCredentialsProvider,
)
from alibabacloud_credentials_api import ICredentialsProvider

from server.aliyun.endpoints import resolve_openapi_endpoint
from server.aliyun.managed_credentials import ManagedCredentialsProvider
from server.aliyun.safe_ram_role import SafeRamRoleArnCredentialsProvider
from server.config import AliyunConfig


def _require_nonblank(name: str, value: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


@dataclass(frozen=True)
class CredentialProbe:
    mode: str
    provider_name: str
    expires_at: int | None
    role_name: str | None


class AliyunCredentialProvider:
    """Official Alibaba Cloud credential client plus non-secret metadata."""

    mode: str
    region_id: str
    openapi_network: str
    credential_client: CredentialsClient
    managed_provider: ManagedCredentialsProvider
    role_name: str | None

    def __init__(
        self,
        raw_provider: ICredentialsProvider,
        *,
        mode: str,
        region_id: str,
        openapi_network: str,
        role_name: str | None = None,
    ) -> None:
        self.mode = mode
        self.region_id = region_id
        self.openapi_network = openapi_network
        self.role_name = role_name
        self.managed_provider = ManagedCredentialsProvider(
            raw_provider, credential_mode=mode
        )
        self.credential_client = CredentialsClient(provider=self.managed_provider)

    def probe(self) -> CredentialProbe:
        credentials = self.managed_provider.cached_credentials
        return CredentialProbe(
            mode=self.mode,
            provider_name=self.managed_provider.get_provider_name(),
            expires_at=(
                credentials.get_expiration() if credentials is not None else None
            ),
            role_name=self.role_name,
        )

    async def get_credentials(self):
        """Compatibility shim for callers performing an explicit probe."""
        return await self.managed_provider.get_credentials_async()


CredentialProvider = AliyunCredentialProvider


class DirectAKProvider(AliyunCredentialProvider):
    def __init__(
        self,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        region_id: str = "cn-hangzhou",
        openapi_network: str = "public",
        *,
        ak: str | None = None,
        sk: str | None = None,
    ) -> None:
        access_key_id = access_key_id if access_key_id is not None else ak
        access_key_secret = (
            access_key_secret if access_key_secret is not None else sk
        )
        access_key_id = _require_nonblank("access_key_id", access_key_id)
        access_key_secret = _require_nonblank(
            "access_key_secret", access_key_secret
        )
        super().__init__(
            StaticAKCredentialsProvider(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
            ),
            mode="direct_ak",
            region_id=region_id,
            openapi_network=openapi_network,
        )


class AssumeRoleProvider(AliyunCredentialProvider):
    def __init__(
        self,
        source_access_key_id: str | None = None,
        source_access_key_secret: str | None = None,
        role_arn: str = "",
        role_session_name: str = "polardb-agentic",
        duration_seconds: int = 3600,
        external_id: str | None = None,
        region_id: str = "cn-hangzhou",
        openapi_network: str = "public",
        *,
        ak: str | None = None,
        sk: str | None = None,
        session_name: str | None = None,
        duration: int | None = None,
    ) -> None:
        source_access_key_id = (
            source_access_key_id
            if source_access_key_id is not None
            else ak
        )
        source_access_key_secret = (
            source_access_key_secret
            if source_access_key_secret is not None
            else sk
        )
        if session_name is not None:
            role_session_name = session_name
        if duration is not None:
            duration_seconds = duration
        source_access_key_id = _require_nonblank(
            "source_access_key_id", source_access_key_id
        )
        source_access_key_secret = _require_nonblank(
            "source_access_key_secret", source_access_key_secret
        )
        role_arn = _require_nonblank("role_arn", role_arn)
        role_session_name = _require_nonblank(
            "role_session_name", role_session_name
        )
        source_provider = StaticAKCredentialsProvider(
            access_key_id=source_access_key_id,
            access_key_secret=source_access_key_secret,
        )
        super().__init__(
            SafeRamRoleArnCredentialsProvider(
                credentials_provider=source_provider,
                role_arn=role_arn,
                role_session_name=role_session_name,
                duration_seconds=duration_seconds,
                external_id=external_id,
                sts_endpoint=resolve_openapi_endpoint(
                    "sts", region_id, openapi_network
                ),
                http_options=HttpOptions(proxy=None),
            ),
            mode="assume_role",
            region_id=region_id,
            openapi_network=openapi_network,
        )


class ECSRamRoleProvider(AliyunCredentialProvider):
    def __init__(
        self,
        role_name: str | None,
        region_id: str = "cn-hangzhou",
        openapi_network: str = "public",
    ) -> None:
        if role_name is not None:
            role_name = _require_nonblank("role_name", role_name)
        raw_provider = EcsRamRoleCredentialsProvider(
            role_name=role_name,
            disable_imds_v1=True,
            async_update_enabled=False,
            http_options=HttpOptions(proxy=None),
        )
        if role_name is None:
            # The SDK snapshots ALIBABA_CLOUD_ECS_METADATA in its constructor.
            # Clear that ambient fallback so it uses fixed-host IMDSv2 discovery.
            raw_provider._role_name = None
        super().__init__(
            raw_provider,
            mode="ecs_ram_role",
            region_id=region_id,
            openapi_network=openapi_network,
            role_name=role_name,
        )


def build_credential_provider(config: AliyunConfig) -> AliyunCredentialProvider:
    if config.credential_mode == "assume_role":
        if config.assume_role is None:
            raise ValueError("assume_role credentials are not configured")
        return AssumeRoleProvider(
            source_access_key_id=config.assume_role.source_access_key_id,
            source_access_key_secret=config.assume_role.source_access_key_secret,
            role_arn=config.assume_role.role_arn,
            role_session_name=config.assume_role.role_session_name,
            duration_seconds=config.assume_role.duration_seconds,
            external_id=config.assume_role.external_id,
            region_id=config.region_id,
            openapi_network=config.openapi_network,
        )
    if config.credential_mode == "ecs_ram_role":
        return ECSRamRoleProvider(
            role_name=(
                config.ecs_ram_role.role_name
                if config.ecs_ram_role is not None
                else None
            ),
            region_id=config.region_id,
            openapi_network=config.openapi_network,
        )
    if config.direct_ak is None:
        raise ValueError("direct_ak credentials are not configured")
    return DirectAKProvider(
        access_key_id=config.direct_ak.access_key_id,
        access_key_secret=config.direct_ak.access_key_secret,
        region_id=config.region_id,
        openapi_network=config.openapi_network,
    )
