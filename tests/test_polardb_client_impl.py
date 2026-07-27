from __future__ import annotations

from unittest.mock import patch

from server.aliyun.credential_provider import DirectAKProvider
from server.aliyun.polardb_client_impl import AliyunPolarDBClient


async def test_sdk_uses_vpc_endpoint_for_vpc_network() -> None:
    provider = DirectAKProvider(
        ak="ak",
        sk="sk",
        region_id="cn-beijing",
        openapi_network="vpc",
    )
    client = AliyunPolarDBClient(provider)

    with patch(
        "alibabacloud_polardb20170801.client.Client"
    ) as client_type:
        await client._get_sdk()

    sdk_config = client_type.call_args.args[0]
    assert sdk_config.endpoint == "polardb-vpc.cn-beijing.aliyuncs.com"
