from __future__ import annotations

import os

import pytest

from server.aliyun.credential_provider import build_credential_provider
from server.aliyun.ecs_metadata import (
    ECSMetadataError,
    resolve_ecs_role_name_v2,
)
from server.config import AliyunConfig


pytestmark = pytest.mark.integration


async def _integration_config() -> AliyunConfig:
    mode = os.environ.get("PAS_TEST_ALIYUN_CREDENTIAL_MODE")
    if mode not in {"direct_ak", "assume_role", "ecs_ram_role"}:
        pytest.skip(
            "PAS_TEST_ALIYUN_CREDENTIAL_MODE must be direct_ak, assume_role, or ecs_ram_role"
        )
    if os.environ.get("PAS_RUN_ALIYUN_INTEGRATION") != "1":
        pytest.skip("PAS_RUN_ALIYUN_INTEGRATION=1 is required for real-cloud tests")
    region_id = os.environ.get("PAS_TEST_ALIYUN_REGION_ID", "cn-hangzhou")
    if mode == "direct_ak":
        key_id = os.environ.get("PAS_TEST_ALIYUN_ACCESS_KEY_ID")
        key_secret = os.environ.get("PAS_TEST_ALIYUN_ACCESS_KEY_SECRET")
        if not key_id or not key_secret:
            pytest.skip("direct_ak requires PAS_TEST_ALIYUN_ACCESS_KEY_ID and PAS_TEST_ALIYUN_ACCESS_KEY_SECRET")
        return AliyunConfig(
            credential_mode=mode,
            region_id=region_id,
            direct_ak={"access_key_id": key_id, "access_key_secret": key_secret},
        )
    if mode == "assume_role":
        key_id = os.environ.get("PAS_TEST_ALIYUN_SOURCE_ACCESS_KEY_ID")
        key_secret = os.environ.get("PAS_TEST_ALIYUN_SOURCE_ACCESS_KEY_SECRET")
        role_arn = os.environ.get("PAS_TEST_ALIYUN_ROLE_ARN")
        if not key_id or not key_secret or not role_arn:
            pytest.skip("assume_role requires source key variables and PAS_TEST_ALIYUN_ROLE_ARN")
        return AliyunConfig(
            credential_mode=mode,
            region_id=region_id,
            assume_role={
                "source_access_key_id": key_id,
                "source_access_key_secret": key_secret,
                "role_arn": role_arn,
                "role_session_name": "pas-integration-test",
                "external_id": os.environ.get("PAS_TEST_ALIYUN_EXTERNAL_ID"),
            },
        )
    try:
        role_name = await resolve_ecs_role_name_v2()
    except ECSMetadataError:
        pytest.skip("ecs_ram_role requires ECS IMDSv2 and an attached RAM role")
    return AliyunConfig(
        credential_mode=mode,
        region_id=region_id,
        ecs_ram_role={"role_name": role_name},
    )


async def test_real_cloud_credentials_can_describe_polardb_clusters() -> None:
    config = await _integration_config()
    provider = build_credential_provider(config)
    from server.aliyun.polardb_client_impl import AliyunPolarDBClient

    clusters = await AliyunPolarDBClient(provider).discover_clusters(config.region_id)
    assert isinstance(clusters, list)
