from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AliyunCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str | None = None
    region_id: str = "cn-hangzhou"


class CredentialProvider(abc.ABC):
    @abc.abstractmethod
    async def get_credentials(self) -> AliyunCredentials: ...


class DirectAKProvider(CredentialProvider):
    def __init__(self, ak: str, sk: str, region_id: str = "cn-hangzhou"):
        self._ak = ak
        self._sk = sk
        self._region_id = region_id

    async def get_credentials(self) -> AliyunCredentials:
        return AliyunCredentials(
            access_key_id=self._ak,
            access_key_secret=self._sk,
            region_id=self._region_id,
        )


async def _call_sts_assume_role(ak: str, sk: str, role_arn: str,
                                 session_name: str, duration: int):
    from alibabacloud_sts20150401.client import Client as StsClient  # type: ignore[import-untyped]
    from alibabacloud_sts20150401.models import AssumeRoleRequest  # type: ignore[import-untyped]
    from alibabacloud_tea_openapi.models import Config  # type: ignore[import-untyped]

    sts = StsClient(Config(
        access_key_id=ak,
        access_key_secret=sk,
        endpoint="sts.aliyuncs.com",
    ))
    return await sts.assume_role_async(AssumeRoleRequest(
        role_arn=role_arn,
        role_session_name=session_name,
        duration_seconds=duration,
    ))


class AssumeRoleProvider(CredentialProvider):
    def __init__(self, ak: str, sk: str, role_arn: str,
                 session_name: str = "polardb-agentic",
                 duration: int = 3600, region_id: str = "cn-hangzhou"):
        self._ak = ak
        self._sk = sk
        self._role_arn = role_arn
        self._session_name = session_name
        self._duration = duration
        self._region_id = region_id
        self._cache: AliyunCredentials | None = None
        self._expires_at: float = 0

    async def get_credentials(self) -> AliyunCredentials:
        if self._cache and time.monotonic() < self._expires_at - 300:
            return self._cache
        resp = await _call_sts_assume_role(
            self._ak, self._sk, self._role_arn,
            self._session_name, self._duration,
        )
        cred = resp.body.credentials
        self._cache = AliyunCredentials(
            access_key_id=cred.access_key_id,
            access_key_secret=cred.access_key_secret,
            security_token=cred.security_token,
            region_id=self._region_id,
        )
        self._expires_at = time.monotonic() + self._duration
        return self._cache


async def build_credential_provider(session: AsyncSession) -> CredentialProvider:
    from server.core.settings_manager import get_setting, get_setting_raw
    mode = await get_setting(session, "aliyun_credential_mode") or "direct_ak"
    ak = await get_setting_raw(session, "aliyun_access_key_id") or ""
    sk = await get_setting_raw(session, "aliyun_access_key_secret") or ""
    region = await get_setting(session, "pool_region_id") or "cn-hangzhou"

    if mode == "assume_role":
        role_arn = await get_setting(session, "aliyun_role_arn") or ""
        session_name = await get_setting(session, "aliyun_role_session_name") or "polardb-agentic"
        duration = int(await get_setting(session, "aliyun_sts_duration_seconds") or "3600")
        return AssumeRoleProvider(ak, sk, role_arn, session_name, duration, region)
    return DirectAKProvider(ak, sk, region)
