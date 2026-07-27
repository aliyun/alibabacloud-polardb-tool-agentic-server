"""Reviewed Alibaba Cloud OpenAPI endpoint resolution."""

from __future__ import annotations

import re
from typing import Literal

OpenAPIService = Literal["polardb", "sts"]
OpenAPINetwork = Literal["public", "vpc"]

_REGION_ID_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+$")
_SERVICE_PREFIXES: dict[str, dict[str, str]] = {
    "sts": {"public": "sts", "vpc": "sts-vpc"},
}

# PolarDB public endpoints do not follow one universal regional naming rule.
# Keep the official endpoint list explicit; unknown valid regions fall back to
# the standard regional form.
POLARDB_ENDPOINTS: dict[str, tuple[str, str]] = {
    "cn-hangzhou": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-hangzhou.aliyuncs.com",
    ),
    "cn-shanghai": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-shanghai.aliyuncs.com",
    ),
    "cn-beijing": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-beijing.aliyuncs.com",
    ),
    "cn-wulanchabu": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-wulanchabu.aliyuncs.com",
    ),
    "cn-heyuan": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-heyuan.aliyuncs.com",
    ),
    "cn-hangzhou-finance": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-hangzhou-finance.aliyuncs.com",
    ),
    "cn-beijing-finance-1": (
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-beijing-finance-1.aliyuncs.com",
    ),
    "cn-qingdao": (
        "polardb.cn-qingdao.aliyuncs.com",
        "polardb-vpc.cn-qingdao.aliyuncs.com",
    ),
    "cn-zhangjiakou": (
        "polardb.cn-zhangjiakou.aliyuncs.com",
        "polardb-vpc.cn-zhangjiakou.aliyuncs.com",
    ),
    "cn-huhehaote": (
        "polardb.cn-huhehaote.aliyuncs.com",
        "polardb-vpc.cn-huhehaote.aliyuncs.com",
    ),
    "cn-shenzhen": (
        "polardb.cn-shenzhen.aliyuncs.com",
        "polardb-vpc.cn-shenzhen.aliyuncs.com",
    ),
    "cn-guangzhou": (
        "polardb.cn-guangzhou.aliyuncs.com",
        "polardb-vpc.cn-guangzhou.aliyuncs.com",
    ),
    "cn-chengdu": (
        "polardb.cn-chengdu.aliyuncs.com",
        "polardb-vpc.cn-chengdu.aliyuncs.com",
    ),
    "cn-hongkong": (
        "polardb.cn-hongkong.aliyuncs.com",
        "polardb-vpc.cn-hongkong.aliyuncs.com",
    ),
    "cn-shanghai-finance-1": (
        "polardb.cn-shanghai-finance-1.aliyuncs.com",
        "polardb-vpc.cn-shanghai-finance-1.aliyuncs.com",
    ),
    "cn-shenzhen-finance-1": (
        "polardb.cn-shenzhen-finance-1.aliyuncs.com",
        "polardb-vpc.cn-shenzhen-finance-1.aliyuncs.com",
    ),
    "ap-northeast-1": (
        "polardb.ap-northeast-1.aliyuncs.com",
        "polardb-vpc.ap-northeast-1.aliyuncs.com",
    ),
    "ap-northeast-2": (
        "polardb.ap-northeast-2.aliyuncs.com",
        "polardb-vpc.ap-northeast-2.aliyuncs.com",
    ),
    "ap-southeast-1": (
        "polardb.ap-southeast-1.aliyuncs.com",
        "polardb-vpc.ap-southeast-1.aliyuncs.com",
    ),
    "ap-southeast-3": (
        "polardb.ap-southeast-3.aliyuncs.com",
        "polardb-vpc.ap-southeast-3.aliyuncs.com",
    ),
    "ap-southeast-5": (
        "polardb.ap-southeast-5.aliyuncs.com",
        "polardb-vpc.ap-southeast-5.aliyuncs.com",
    ),
    "ap-southeast-6": (
        "polardb.ap-southeast-6.aliyuncs.com",
        "polardb-vpc.ap-southeast-6.aliyuncs.com",
    ),
    "ap-southeast-7": (
        "polardb.ap-southeast-7.aliyuncs.com",
        "polardb-vpc.ap-southeast-7.aliyuncs.com",
    ),
    "us-east-1": (
        "polardb.us-east-1.aliyuncs.com",
        "polardb-vpc.us-east-1.aliyuncs.com",
    ),
    "us-west-1": (
        "polardb.us-west-1.aliyuncs.com",
        "polardb-vpc.us-west-1.aliyuncs.com",
    ),
    "eu-west-1": (
        "polardb.eu-west-1.aliyuncs.com",
        "polardb-vpc.eu-west-1.aliyuncs.com",
    ),
    "eu-central-1": (
        "polardb.eu-central-1.aliyuncs.com",
        "polardb-vpc.eu-central-1.aliyuncs.com",
    ),
    "me-east-1": (
        "polardb.me-east-1.aliyuncs.com",
        "polardb-vpc.me-east-1.aliyuncs.com",
    ),
    "na-south-1": (
        "polardb.na-south-1.aliyuncs.com",
        "polardb-vpc.na-south-1.aliyuncs.com",
    ),
}


class OpenAPIEndpointError(ValueError):
    """Raised when an OpenAPI endpoint cannot be safely resolved."""


def _validate_region_and_network(region_id: str, network: str) -> None:
    if network not in {"public", "vpc"}:
        raise OpenAPIEndpointError(
            f"Unsupported OpenAPI network: {network}"
        )
    if not _REGION_ID_PATTERN.fullmatch(region_id):
        raise OpenAPIEndpointError("Invalid Alibaba Cloud region ID")


def resolve_polardb_endpoint(
    region_id: str,
    network: str = "public",
) -> str:
    """Return the official PolarDB endpoint for a region and network."""
    _validate_region_and_network(region_id, network)
    entry = POLARDB_ENDPOINTS.get(region_id)
    if network == "vpc":
        return (
            entry[1]
            if entry
            else f"polardb-vpc.{region_id}.aliyuncs.com"
        )
    return (
        entry[0]
        if entry
        else f"polardb.{region_id}.aliyuncs.com"
    )


def resolve_openapi_endpoint(
    service: str,
    region_id: str,
    network: str,
) -> str:
    """Resolve a reviewed Alibaba Cloud public or VPC endpoint."""
    if service == "polardb":
        return resolve_polardb_endpoint(region_id, network)
    service_endpoints = _SERVICE_PREFIXES.get(service)
    if service_endpoints is None:
        raise OpenAPIEndpointError(
            f"Unsupported OpenAPI service: {service}"
        )
    _validate_region_and_network(region_id, network)
    return f"{service_endpoints[network]}.{region_id}.aliyuncs.com"
