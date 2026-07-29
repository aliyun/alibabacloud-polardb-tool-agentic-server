from __future__ import annotations

import pytest

from server.configuration.registry import (
    MODULE_REGISTRY,
    topological_modules,
    validate_module_config,
)


def test_registry_contains_exact_initial_modules() -> None:
    assert set(MODULE_REGISTRY) == {
        "core_admin",
        "agent_token_auth",
        "user_sso",
        "aliyun_access",
        "agentic_db_purchase",
        "resource_pool",
        "runtime_policy",
        "sql_security",
        "observability",
        "token_security",
    }


def test_dependency_graph_is_acyclic_and_stable() -> None:
    order = topological_modules(MODULE_REGISTRY)

    assert set(order) == set(MODULE_REGISTRY)
    assert order.index("token_security") < order.index("core_admin")
    assert order.index("token_security") < order.index("user_sso")
    assert order.index("aliyun_access") < order.index(
        "agentic_db_purchase"
    )
    assert order.index("agentic_db_purchase") < order.index(
        "resource_pool"
    )


def test_sso_requires_external_https_url() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
        },
        effective_configs={
            "runtime_policy": {"external_base_url": None}
        },
    )

    assert result.valid is False
    assert result.error_code == "EXTERNAL_BASE_URL_REQUIRED"


def test_sso_accepts_https_external_url() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is True
    assert result.normalized_config["provider_name"] == "oidc"


def test_runtime_poll_interval_is_bounded() -> None:
    result = validate_module_config(
        "runtime_policy",
        {"config_poll_interval_seconds": 0},
        effective_configs={},
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_secret_fields_are_code_owned() -> None:
    assert MODULE_REGISTRY["user_sso"].secret_fields == (
        "client_secret",
    )
    assert MODULE_REGISTRY["aliyun_access"].secret_fields == (
        "access_key_id",
        "access_key_secret",
    )
    assert "private_key" in MODULE_REGISTRY["token_security"].secret_fields


def test_aliyun_access_defaults_to_public_openapi_network() -> None:
    result = validate_module_config(
        "aliyun_access",
        {
            "access_key_id": "test-ak",
            "access_key_secret": "test-sk",
        },
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["openapi_network"] == "public"


def test_aliyun_access_accepts_vpc_openapi_network() -> None:
    result = validate_module_config(
        "aliyun_access",
        {
            "access_key_id": "test-ak",
            "access_key_secret": "test-sk",
            "openapi_network": "vpc",
        },
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["openapi_network"] == "vpc"


@pytest.mark.parametrize(
    ("vpc_id", "vswitch_id"),
    [
        ("", ""),
        ("vpc-bp-example", ""),
        ("", "vsw-bp-example"),
    ],
)
def test_resource_pool_requires_explicit_vpc_and_vswitch(
    vpc_id: str,
    vswitch_id: str,
) -> None:
    result = validate_module_config(
        "resource_pool",
        {
            "region_id": "cn-hangzhou",
            "zone_id": "cn-hangzhou-j",
            "vpc_id": vpc_id,
            "vswitch_id": vswitch_id,
        },
        effective_configs={},
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_resource_pool_describes_vpc_reachability_requirement() -> None:
    schema = MODULE_REGISTRY[
        "resource_pool"
    ].model.model_json_schema()

    assert "PAS" in schema["properties"]["vpc_id"]["description"]
    assert "reachable" in schema["properties"]["vpc_id"]["description"]
