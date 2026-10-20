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
        ("polardb", "cn-unknown", "public"),
        ("polardb", "cn-unknown", "vpc"),
        ("sts", "cn-unknown", "vpc"),
        ("polardb", "cn-beijing-finance-1", "vpc"),
        ("sts", "cn-hangzhou-finance", "vpc"),
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


def test_catalog_includes_johor_and_explicit_unsupported_endpoints():
    from server.aliyun.endpoints import endpoint_catalog, resolve_openapi_endpoint

    catalog = endpoint_catalog()
    assert catalog["reviewed_at"] == "2026-09-24"
    regions = {entry["region_id"]: entry for entry in catalog["regions"]}
    assert len(regions) == 30
    assert regions["ap-southeast-8"]["label"]["zh-CN"] == "马来西亚（柔佛州）"
    assert resolve_openapi_endpoint("polardb", "ap-southeast-8", "vpc") == (
        "polardb-vpc.ap-southeast-8.aliyuncs.com"
    )
    assert regions["cn-beijing-finance-1"]["polardb"]["vpc"] is None
    assert regions["cn-hangzhou-finance"]["sts"]["vpc"] is None
