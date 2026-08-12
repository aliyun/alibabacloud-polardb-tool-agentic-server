from __future__ import annotations

import pytest

from server import config as config_module
from server.aliyun.credential_provider import DirectAKProvider
from server.aliyun.polardb_client_impl import AliyunPolarDBClient
from server.config import AppConfig, reset_config


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


class _StaticProvider(DirectAKProvider):
    def __init__(
        self,
        region_id: str,
        openapi_network: str = "public",
    ) -> None:
        super().__init__(
            access_key_id="TEST_ACCESS_KEY_ID",
            access_key_secret="TEST_CREDENTIAL_VALUE_123",
            region_id=region_id,
            openapi_network=openapi_network,
        )

    def set_network(self, openapi_network: str) -> None:
        self.openapi_network = openapi_network


def _install_config() -> None:
    config_module._config = AppConfig(
        aliyun={
            "access_key_id": "TEST_ACCESS_KEY_ID",
            "access_key_secret": "TEST_CREDENTIAL_VALUE_123",
        },
    )


def test_endpoint_static_provider_uses_current_credentials_sdk_contract():
    provider = _StaticProvider("cn-hangzhou")

    assert provider.credential_client is not None
    provider.set_network("vpc")
    assert provider.openapi_network == "vpc"


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
