from __future__ import annotations

from server.configuration.registry import (
    MODULE_REGISTRY,
    topological_modules,
    validate_module_config,
)
from server.configuration.secrets import SecretFieldSpec
from server.configuration.types import ModuleState


def test_registry_contains_exact_initial_modules() -> None:
    assert set(MODULE_REGISTRY) == {
        "core_admin",
        "agent_token_auth",
        "user_sso",
        "aliyun_access",
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
    assert "agentic_db_purchase" not in order
    assert "resource_pool" not in order


def test_agent_token_auth_is_an_active_required_capability() -> None:
    definition = MODULE_REGISTRY["agent_token_auth"]

    assert definition.initial_state == ModuleState.ACTIVE
    assert definition.optional is False
    assert definition.system_owned is True
    assert definition.configurable is False


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
        SecretFieldSpec("client_secret"),
    )
    assert MODULE_REGISTRY["aliyun_access"].secret_fields == (
        SecretFieldSpec("direct_ak.access_key_id", display_mask=True),
        SecretFieldSpec("direct_ak.access_key_secret"),
        SecretFieldSpec(
            "assume_role.source_access_key_id", display_mask=True
        ),
        SecretFieldSpec("assume_role.source_access_key_secret"),
        SecretFieldSpec("assume_role.external_id"),
    )
    assert SecretFieldSpec("private_key") in MODULE_REGISTRY[
        "token_security"
    ].secret_fields


def test_aliyun_access_uses_schema_v2() -> None:
    assert MODULE_REGISTRY["aliyun_access"].schema_version == 2


def test_aliyun_access_defaults_to_public_openapi_network() -> None:
    result = validate_module_config(
        "aliyun_access",
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "test-ak",
                "access_key_secret": "test-sk",
            },
        },
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["openapi_network"] == "public"


def test_aliyun_access_accepts_vpc_openapi_network() -> None:
    result = validate_module_config(
        "aliyun_access",
        {
            "credential_mode": "direct_ak",
            "direct_ak": {
                "access_key_id": "test-ak",
                "access_key_secret": "test-sk",
            },
            "openapi_network": "vpc",
        },
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["openapi_network"] == "vpc"


def test_resource_pool_module_is_retired() -> None:
    assert "resource_pool" not in MODULE_REGISTRY
