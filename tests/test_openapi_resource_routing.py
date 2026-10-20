from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.aliyun.credential_provider import DirectAKProvider
from server.aliyun.endpoints import OpenAPIEndpointError
from server.aliyun.polardb_client_impl import AliyunPolarDBClient
from server.core import binding_manager
from server.models import Instance, Permission, User


def _session_without_credential():
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    return session


async def test_account_creation_routes_by_instance_region(monkeypatch):
    client = AliyunPolarDBClient(DirectAKProvider(
        access_key_id="test-key", access_key_secret="test-secret",
        region_id="cn-hangzhou",
    ))
    sdk = AsyncMock()
    scoped = client.for_region("cn-shenzhen")
    monkeypatch.setattr(scoped, "_get_sdk", AsyncMock(return_value=sdk))
    monkeypatch.setattr(
        binding_manager, "get_polardb_client_async", AsyncMock(return_value=client),
    )
    await binding_manager._create_instance_credential(
        _session_without_credential(),
        Instance(id="test-instance", cluster_id="pc-test", region="cn-shenzhen"),
        User(id="test-user"), Permission.READWRITE, encryption_key=b"x" * 32,
    )
    sdk.create_account_async.assert_awaited_once()
    assert client._credential_provider.region_id == "cn-hangzhou"


@pytest.mark.parametrize("region", [None, "cn-shenzhen"])
async def test_account_creation_rejects_unknown_or_cross_vpc_region(monkeypatch, region):
    client = AliyunPolarDBClient(DirectAKProvider(
        access_key_id="test-key", access_key_secret="test-secret",
        region_id="cn-hangzhou", openapi_network="vpc",
    ))
    network = AsyncMock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(client, "_get_sdk", network)
    monkeypatch.setattr(
        binding_manager, "get_polardb_client_async", AsyncMock(return_value=client),
    )
    with pytest.raises(OpenAPIEndpointError):
        await binding_manager._create_instance_credential(
            _session_without_credential(), Instance(cluster_id="pc-test", region=region),
            User(id="test-user"), Permission.READWRITE,
        )
    network.assert_not_awaited()


async def test_discovery_request_and_sdk_endpoint_use_same_resource_region(monkeypatch):
    created = []

    class SDK:
        def __init__(self, config):
            created.append(config)

        async def describe_dbclusters_async(self, request):
            assert request.region_id == "cn-shenzhen"
            return SimpleNamespace(body=SimpleNamespace(
                items=SimpleNamespace(dbcluster=[]), request_id="test-request",
            ))

    monkeypatch.setattr("alibabacloud_polardb20170801.client.Client", SDK)
    provider = DirectAKProvider(access_key_id="key", access_key_secret="secret", region_id="cn-hangzhou")
    client = AliyunPolarDBClient(provider)
    assert await client.discover_clusters("cn-shenzhen") == []
    assert created[0].region_id == "cn-shenzhen"
    assert created[0].endpoint == "polardb.cn-shenzhen.aliyuncs.com"
    assert provider.region_id == "cn-hangzhou"
