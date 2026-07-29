from __future__ import annotations

import pytest

from server import config as config_module
from server.aliyun.credential_provider import AliyunCredentials
from server.aliyun.polardb_client_impl import AliyunPolarDBClient
from server.config import AppConfig, reset_config
from server.core.provisioner import _preflight_check


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


class _StaticProvider:
    def __init__(
        self,
        region_id: str,
        openapi_network: str = "public",
    ) -> None:
        self._cred = AliyunCredentials(
            access_key_id="TEST_ACCESS_KEY_ID",
            access_key_secret="TEST_CREDENTIAL_VALUE_123",
            region_id=region_id,
            openapi_network=openapi_network,
        )

    async def get_credentials(self) -> AliyunCredentials:
        return self._cred

    def set_network(self, openapi_network: str) -> None:
        self._cred = AliyunCredentials(
            access_key_id=self._cred.access_key_id,
            access_key_secret=self._cred.access_key_secret,
            region_id=self._cred.region_id,
            openapi_network=openapi_network,
        )


def _install_config(**pool: object) -> None:
    config_module._config = AppConfig(
        aliyun={
            "access_key_id": "TEST_ACCESS_KEY_ID",
            "access_key_secret": "TEST_CREDENTIAL_VALUE_123",
        },
        polardb={"resource_pool": pool},
    )


class TestOpenAPIEndpointResolution:
    async def test_public_central_region_uses_shared_endpoint(self):
        _install_config()
        client = AliyunPolarDBClient(_StaticProvider("cn-hangzhou"))
        sdk = await client._get_sdk()
        assert sdk._endpoint == "polardb.aliyuncs.com"

    async def test_shanghai_public_is_central_endpoint(self):
        _install_config()
        client = AliyunPolarDBClient(_StaticProvider("cn-shanghai"))
        sdk = await client._get_sdk()
        assert sdk._endpoint == "polardb.aliyuncs.com"

    async def test_qingdao_public_is_regional_per_official_list(self):
        _install_config()
        client = AliyunPolarDBClient(_StaticProvider("cn-qingdao"))
        sdk = await client._get_sdk()
        assert sdk._endpoint == "polardb.cn-qingdao.aliyuncs.com"

    async def test_public_regional_endpoint(self):
        _install_config()
        client = AliyunPolarDBClient(_StaticProvider("cn-shenzhen"))
        sdk = await client._get_sdk()
        assert sdk._endpoint == "polardb.cn-shenzhen.aliyuncs.com"

    async def test_vpc_endpoint_is_explicit(self):
        _install_config()
        client = AliyunPolarDBClient(
            _StaticProvider("cn-hangzhou", "vpc")
        )
        sdk = await client._get_sdk()
        assert sdk._endpoint == "polardb-vpc.cn-hangzhou.aliyuncs.com"

    async def test_unknown_region_falls_back_to_naming_rule(self):
        from server.aliyun.endpoints import resolve_polardb_endpoint

        assert (
            resolve_polardb_endpoint("cn-unknown", "public")
            == "polardb.cn-unknown.aliyuncs.com"
        )
        assert (
            resolve_polardb_endpoint("cn-unknown", "vpc")
            == "polardb-vpc.cn-unknown.aliyuncs.com"
        )

    async def test_sdk_rebuilt_when_endpoint_network_changes(self):
        _install_config()
        provider = _StaticProvider("cn-hangzhou")
        client = AliyunPolarDBClient(provider)
        first = await client._get_sdk()
        provider.set_network("vpc")
        second = await client._get_sdk()
        assert first is not second
        assert second._endpoint == "polardb-vpc.cn-hangzhou.aliyuncs.com"


class TestPreflightNetworkCheck:
    async def test_region_and_zone_required(self):
        _install_config(region_id="", zone_id="")
        result = await _preflight_check(None)
        assert result is not None
        assert "Region/Zone" in result["message"]

    async def test_vpc_and_vswitch_are_required(self):
        _install_config(region_id="cn-hangzhou", zone_id="cn-hangzhou-j")
        result = await _preflight_check(None)
        assert result is not None
        assert "VPC/VSwitch" in result["message"]

    async def test_explicit_vpc_and_vswitch_pass(self):
        _install_config(
            region_id="cn-hangzhou",
            zone_id="cn-hangzhou-j",
            vpc_id="vpc-bp-example",
            vswitch_id="vsw-bp-example",
        )
        assert await _preflight_check(None) is None
