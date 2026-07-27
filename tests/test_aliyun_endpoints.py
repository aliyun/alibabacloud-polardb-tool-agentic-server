from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("service", "network", "expected"),
    [
        ("polardb", "public", "polardb.aliyuncs.com"),
        ("polardb", "vpc", "polardb-vpc.cn-beijing.aliyuncs.com"),
        ("sts", "public", "sts.cn-beijing.aliyuncs.com"),
        ("sts", "vpc", "sts-vpc.cn-beijing.aliyuncs.com"),
    ],
)
def test_resolve_openapi_endpoint(
    service: str,
    network: str,
    expected: str,
) -> None:
    from server.aliyun.endpoints import resolve_openapi_endpoint

    assert resolve_openapi_endpoint(
        service, "cn-beijing", network
    ) == expected


@pytest.mark.parametrize(
    ("service", "region", "network"),
    [
        ("unknown", "cn-beijing", "public"),
        ("polardb", "invalid.example.com", "public"),
        ("polardb", "cn-beijing", "custom"),
    ],
)
def test_resolver_rejects_unreviewed_inputs(
    service: str,
    region: str,
    network: str,
) -> None:
    from server.aliyun.endpoints import OpenAPIEndpointError
    from server.aliyun.endpoints import resolve_openapi_endpoint

    with pytest.raises(OpenAPIEndpointError):
        resolve_openapi_endpoint(service, region, network)
