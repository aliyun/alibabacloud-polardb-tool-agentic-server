from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from server.aliyun.endpoints import resolve_openapi_endpoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AliyunCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str | None = None
    region_id: str = "cn-hangzhou"
    openapi_network: str = "public"


class CredentialProvider(abc.ABC):
    @abc.abstractmethod
    async def get_credentials(self) -> AliyunCredentials: ...


class DirectAKProvider(CredentialProvider):
    def __init__(
        self,
        ak: str,
        sk: str,
        region_id: str = "cn-hangzhou",
        openapi_network: str = "public",
    ):
        self._ak = ak
        self._sk = sk
        self._region_id = region_id
        self._openapi_network = openapi_network

    async def get_credentials(self) -> AliyunCredentials:
        return AliyunCredentials(
            access_key_id=self._ak,
            access_key_secret=self._sk,
            region_id=self._region_id,
            openapi_network=self._openapi_network,
        )


async def _call_sts_assume_role(
    ak: str,
    sk: str,
    role_arn: str,
    session_name: str,
    duration: int,
    endpoint: str,
):
    from alibabacloud_sts20150401.client import Client as StsClient  # type: ignore[import-untyped]
    from alibabacloud_sts20150401.models import AssumeRoleRequest  # type: ignore[import-untyped]
    from alibabacloud_tea_openapi.models import Config  # type: ignore[import-untyped]

    sts = StsClient(Config(
        access_key_id=ak,
        access_key_secret=sk,
        endpoint=endpoint,
    ))
    return await sts.assume_role_async(AssumeRoleRequest(
        role_arn=role_arn,
        role_session_name=session_name,
        duration_seconds=duration,
    ))


class AssumeRoleProvider(CredentialProvider):
    def __init__(
        self,
        ak: str,
        sk: str,
        role_arn: str,
        session_name: str = "polardb-agentic",
        duration: int = 3600,
        region_id: str = "cn-hangzhou",
        openapi_network: str = "public",
    ):
        self._ak = ak
        self._sk = sk
        self._role_arn = role_arn
        self._session_name = session_name
        self._duration = duration
        self._region_id = region_id
        self._openapi_network = openapi_network
        self._cache: AliyunCredentials | None = None
        self._expires_at: float = 0

    async def get_credentials(self) -> AliyunCredentials:
        if self._cache and time.monotonic() < self._expires_at - 300:
            return self._cache
        resp = await _call_sts_assume_role(
            self._ak, self._sk, self._role_arn,
            self._session_name, self._duration,
            resolve_openapi_endpoint(
                "sts", self._region_id, self._openapi_network
            ),
        )
        cred = resp.body.credentials
        self._cache = AliyunCredentials(
            access_key_id=cred.access_key_id,
            access_key_secret=cred.access_key_secret,
            security_token=cred.security_token,
            region_id=self._region_id,
            openapi_network=self._openapi_network,
        )
        self._expires_at = time.monotonic() + self._duration
        return self._cache


async def build_credential_provider(session: AsyncSession) -> CredentialProvider:
    from server.config import get_config

    aliyun = get_config().aliyun
    mode = aliyun.credential_mode
    ak = aliyun.access_key_id
    sk = aliyun.access_key_secret
    region = aliyun.region_id
    network = aliyun.openapi_network

    if mode == "assume_role":
        return AssumeRoleProvider(
            ak,
            sk,
            aliyun.role_arn,
            aliyun.role_session_name,
            aliyun.sts_duration_seconds,
            region,
            network,
        )
    return DirectAKProvider(ak, sk, region, network)
