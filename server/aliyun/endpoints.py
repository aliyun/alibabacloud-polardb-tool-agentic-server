"""Offline reviewed OpenAPI catalog. Never infer an unlisted endpoint."""

from __future__ import annotations

from typing import Literal

OpenAPIService = Literal["polardb", "sts"]
OpenAPINetwork = Literal["public", "vpc"]
CATALOG_REVIEWED_AT = "2026-09-24"
CATALOG_SOURCES = {
    "polardb": "https://api.aliyun.com/product/polardb?tab=endpoints",
    "sts": "https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-endpoint",
}

# PAS regions are PolarDB regions. None means the official table has no endpoint.
# region, Chinese label, English label, PolarDB public/VPC, STS public/VPC.
_REVIEWED_REGIONS = (
    (
        "cn-qingdao", "华北1（青岛）", "China (Qingdao)",
        "polardb.cn-qingdao.aliyuncs.com",
        "polardb-vpc.cn-qingdao.aliyuncs.com",
        "sts.cn-qingdao.aliyuncs.com",
        "sts-vpc.cn-qingdao.aliyuncs.com",
    ),
    (
        "cn-beijing", "华北2（北京）", "China (Beijing)",
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-beijing.aliyuncs.com",
        "sts.cn-beijing.aliyuncs.com",
        "sts-vpc.cn-beijing.aliyuncs.com",
    ),
    (
        "cn-zhangjiakou", "华北3（张家口）", "China (Zhangjiakou)",
        "polardb.cn-zhangjiakou.aliyuncs.com",
        "polardb-vpc.cn-zhangjiakou.aliyuncs.com",
        "sts.cn-zhangjiakou.aliyuncs.com",
        "sts-vpc.cn-zhangjiakou.aliyuncs.com",
    ),
    (
        "cn-huhehaote", "华北5（呼和浩特）", "China (Hohhot)",
        "polardb.cn-huhehaote.aliyuncs.com",
        "polardb-vpc.cn-huhehaote.aliyuncs.com",
        "sts.cn-huhehaote.aliyuncs.com",
        "sts-vpc.cn-huhehaote.aliyuncs.com",
    ),
    (
        "cn-wulanchabu", "华北6（乌兰察布）", "China (Ulanqab)",
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-wulanchabu.aliyuncs.com",
        "sts.cn-wulanchabu.aliyuncs.com",
        "sts-vpc.cn-wulanchabu.aliyuncs.com",
    ),
    (
        "cn-hangzhou", "华东1（杭州）", "China (Hangzhou)",
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-hangzhou.aliyuncs.com",
        "sts.cn-hangzhou.aliyuncs.com",
        "sts-vpc.cn-hangzhou.aliyuncs.com",
    ),
    (
        "cn-shanghai", "华东2（上海）", "China (Shanghai)",
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-shanghai.aliyuncs.com",
        "sts.cn-shanghai.aliyuncs.com",
        "sts-vpc.cn-shanghai.aliyuncs.com",
    ),
    (
        "cn-shenzhen", "华南1（深圳）", "China (Shenzhen)",
        "polardb.cn-shenzhen.aliyuncs.com",
        "polardb-vpc.cn-shenzhen.aliyuncs.com",
        "sts.cn-shenzhen.aliyuncs.com",
        "sts-vpc.cn-shenzhen.aliyuncs.com",
    ),
    (
        "cn-heyuan", "华南2（河源）", "China (Heyuan)",
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-heyuan.aliyuncs.com",
        "sts.cn-heyuan.aliyuncs.com",
        "sts-vpc.cn-heyuan.aliyuncs.com",
    ),
    (
        "cn-guangzhou", "华南3（广州）", "China (Guangzhou)",
        "polardb.cn-guangzhou.aliyuncs.com",
        "polardb-vpc.cn-guangzhou.aliyuncs.com",
        "sts.cn-guangzhou.aliyuncs.com",
        "sts-vpc.cn-guangzhou.aliyuncs.com",
    ),
    (
        "ap-southeast-6", "菲律宾（马尼拉）", "Philippines (Manila)",
        "polardb.ap-southeast-6.aliyuncs.com",
        "polardb-vpc.ap-southeast-6.aliyuncs.com",
        "sts.ap-southeast-6.aliyuncs.com",
        "sts-vpc.ap-southeast-6.aliyuncs.com",
    ),
    (
        "ap-northeast-2", "韩国（首尔）", "South Korea (Seoul)",
        "polardb.ap-northeast-2.aliyuncs.com",
        "polardb-vpc.ap-northeast-2.aliyuncs.com",
        "sts.ap-northeast-2.aliyuncs.com",
        "sts-vpc.ap-northeast-2.aliyuncs.com",
    ),
    (
        "ap-southeast-3", "马来西亚（吉隆坡）", "Malaysia (Kuala Lumpur)",
        "polardb.ap-southeast-3.aliyuncs.com",
        "polardb-vpc.ap-southeast-3.aliyuncs.com",
        "sts.ap-southeast-3.aliyuncs.com",
        "sts-vpc.ap-southeast-3.aliyuncs.com",
    ),
    (
        "ap-southeast-8", "马来西亚（柔佛州）", "Malaysia (Johor)",
        "polardb.ap-southeast-8.aliyuncs.com",
        "polardb-vpc.ap-southeast-8.aliyuncs.com",
        "sts.ap-southeast-8.aliyuncs.com",
        "sts-vpc.ap-southeast-8.aliyuncs.com",
    ),
    (
        "ap-northeast-1", "日本（东京）", "Japan (Tokyo)",
        "polardb.ap-northeast-1.aliyuncs.com",
        "polardb-vpc.ap-northeast-1.aliyuncs.com",
        "sts.ap-northeast-1.aliyuncs.com",
        "sts-vpc.ap-northeast-1.aliyuncs.com",
    ),
    (
        "ap-southeast-7", "泰国（曼谷）", "Thailand (Bangkok)",
        "polardb.ap-southeast-7.aliyuncs.com",
        "polardb-vpc.ap-southeast-7.aliyuncs.com",
        "sts.ap-southeast-7.aliyuncs.com",
        "sts-vpc.ap-southeast-7.aliyuncs.com",
    ),
    (
        "cn-chengdu", "西南1（成都）", "China (Chengdu)",
        "polardb.cn-chengdu.aliyuncs.com",
        "polardb-vpc.cn-chengdu.aliyuncs.com",
        "sts.cn-chengdu.aliyuncs.com",
        "sts-vpc.cn-chengdu.aliyuncs.com",
    ),
    (
        "ap-southeast-1", "新加坡", "Singapore",
        "polardb.ap-southeast-1.aliyuncs.com",
        "polardb-vpc.ap-southeast-1.aliyuncs.com",
        "sts.ap-southeast-1.aliyuncs.com",
        "sts-vpc.ap-southeast-1.aliyuncs.com",
    ),
    (
        "ap-southeast-5", "印度尼西亚（雅加达）", "Indonesia (Jakarta)",
        "polardb.ap-southeast-5.aliyuncs.com",
        "polardb-vpc.ap-southeast-5.aliyuncs.com",
        "sts.ap-southeast-5.aliyuncs.com",
        "sts-vpc.ap-southeast-5.aliyuncs.com",
    ),
    (
        "cn-hongkong", "中国香港", "China (Hong Kong)",
        "polardb.cn-hongkong.aliyuncs.com",
        "polardb-vpc.cn-hongkong.aliyuncs.com",
        "sts.cn-hongkong.aliyuncs.com",
        "sts-vpc.cn-hongkong.aliyuncs.com",
    ),
    (
        "eu-central-1", "德国（法兰克福）", "Germany (Frankfurt)",
        "polardb.eu-central-1.aliyuncs.com",
        "polardb-vpc.eu-central-1.aliyuncs.com",
        "sts.eu-central-1.aliyuncs.com",
        "sts-vpc.eu-central-1.aliyuncs.com",
    ),
    (
        "us-east-1", "美国（弗吉尼亚）", "US (Virginia)",
        "polardb.us-east-1.aliyuncs.com",
        "polardb-vpc.us-east-1.aliyuncs.com",
        "sts.us-east-1.aliyuncs.com",
        "sts-vpc.us-east-1.aliyuncs.com",
    ),
    (
        "us-west-1", "美国（硅谷）", "US (Silicon Valley)",
        "polardb.us-west-1.aliyuncs.com",
        "polardb-vpc.us-west-1.aliyuncs.com",
        "sts.us-west-1.aliyuncs.com",
        "sts-vpc.us-west-1.aliyuncs.com",
    ),
    (
        "na-south-1", "墨西哥", "Mexico",
        "polardb.na-south-1.aliyuncs.com",
        "polardb-vpc.na-south-1.aliyuncs.com",
        "sts.na-south-1.aliyuncs.com",
        "sts-vpc.na-south-1.aliyuncs.com",
    ),
    (
        "eu-west-1", "英国（伦敦）", "UK (London)",
        "polardb.eu-west-1.aliyuncs.com",
        "polardb-vpc.eu-west-1.aliyuncs.com",
        "sts.eu-west-1.aliyuncs.com",
        "sts-vpc.eu-west-1.aliyuncs.com",
    ),
    (
        "me-east-1", "阿联酋（迪拜）", "UAE (Dubai)",
        "polardb.me-east-1.aliyuncs.com",
        "polardb-vpc.me-east-1.aliyuncs.com",
        "sts.me-east-1.aliyuncs.com",
        "sts-vpc.me-east-1.aliyuncs.com",
    ),
    (
        "cn-beijing-finance-1", "华北2 金融云（邀测）", "China (Beijing) Finance",
        "polardb.aliyuncs.com",
        None,
        "sts.cn-beijing-finance-1.aliyuncs.com",
        "sts-vpc.cn-beijing-finance-1.aliyuncs.com",
    ),
    (
        "cn-hangzhou-finance", "华东1 金融云", "China (Hangzhou) Finance",
        "polardb.aliyuncs.com",
        "polardb-vpc.cn-hangzhou-finance.aliyuncs.com",
        "sts.cn-hangzhou-finance.aliyuncs.com",
        None,
    ),
    (
        "cn-shanghai-finance-1", "华东2 金融云", "China (Shanghai) Finance",
        "polardb.cn-shanghai-finance-1.aliyuncs.com",
        "polardb-vpc.cn-shanghai-finance-1.aliyuncs.com",
        "sts.cn-shanghai-finance-1.aliyuncs.com",
        "sts-vpc.cn-shanghai-finance-1.aliyuncs.com",
    ),
    (
        "cn-shenzhen-finance-1", "华南1 金融云", "China (Shenzhen) Finance",
        "polardb.cn-shenzhen-finance-1.aliyuncs.com",
        "polardb-vpc.cn-shenzhen-finance-1.aliyuncs.com",
        "sts.cn-shenzhen-finance-1.aliyuncs.com",
        "sts-vpc.cn-shenzhen-finance-1.aliyuncs.com",
    ),
)

POLARDB_ENDPOINTS = {row[0]: (row[3], row[4]) for row in _REVIEWED_REGIONS}
STS_ENDPOINTS = {row[0]: (row[5], row[6]) for row in _REVIEWED_REGIONS}


class OpenAPIEndpointError(ValueError):
    """A service/region/network combination is not in the reviewed catalog."""


def endpoint_catalog() -> dict:
    """Return a fresh, credential-free catalog shared by configuration and UI."""
    return {
        "reviewed_at": CATALOG_REVIEWED_AT,
        "sources": dict(CATALOG_SOURCES),
        "regions": [
            {
                "region_id": row[0],
                "label": {"zh-CN": row[1], "en": row[2]},
                "polardb": {"public": row[3], "vpc": row[4]},
                "sts": {"public": row[5], "vpc": row[6]},
            }
            for row in _REVIEWED_REGIONS
        ],
    }


def resolve_openapi_endpoint(service: str, region_id: str, network: str) -> str:
    """Resolve only an explicitly reviewed endpoint, with no network fallback."""
    catalog = {"polardb": POLARDB_ENDPOINTS, "sts": STS_ENDPOINTS}.get(service)
    entry = catalog.get(region_id) if catalog is not None else None
    if network not in {"public", "vpc"} or entry is None:
        raise OpenAPIEndpointError("Unsupported OpenAPI service, region, or network")
    endpoint = entry[0 if network == "public" else 1]
    if endpoint is None:
        raise OpenAPIEndpointError("No official endpoint for this service and network")
    return endpoint


def resolve_polardb_endpoint(region_id: str, network: str = "public") -> str:
    return resolve_openapi_endpoint("polardb", region_id, network)


def validate_resource_region(
    region_id: str | None, *, access_region: str, network: str
) -> str:
    """Resource placement and VPC access location are separate, explicit facts."""
    if not region_id:
        raise OpenAPIEndpointError("A resource region is required for OpenAPI operations")
    resolve_polardb_endpoint(region_id, network)
    if network == "vpc" and region_id != access_region:
        raise OpenAPIEndpointError(
            "VPC OpenAPI resource region must match the PAS access region"
        )
    return region_id
