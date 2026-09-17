from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.config import PolarRAGToolLimitsConfig
from server.configuration.registry import (
    MODULE_REGISTRY,
    topological_modules,
    validate_module_config,
)
from server.configuration.secrets import SecretFieldSpec
from server.configuration.types import ModuleState


def test_registry_contains_exact_initial_modules() -> None:
    assert set(MODULE_REGISTRY) == {
        "knowledge",
        "core_admin",
        "agent_token_auth",
        "user_sso",
        "aliyun_access",
        "runtime_policy",
        "polarrag_tool_limits",
        "enterprise_identity_sync",
        "sql_security",
        "observability",
        "token_security",
    }


def test_polarrag_tool_limits_rejects_non_positive_capacity() -> None:
    result = validate_module_config(
        "polarrag_tool_limits",
        {
            "enabled": True,
            "user_requests_per_minute": 60,
            "user_burst": 10,
            "agent_requests_per_minute": 120,
            "agent_burst": 20,
            "instance_max_inflight": 0,
            "max_fanout": 8,
            "retry_after_seconds": 1,
        },
        effective_configs={},
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_polarrag_tool_limits_rejects_unknown_fields() -> None:
    result = validate_module_config(
        "polarrag_tool_limits",
        {"max_fanotu": 1},
        effective_configs={},
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


@pytest.mark.parametrize(
    "field",
    [
        "user_requests_per_minute",
        "user_burst",
        "agent_requests_per_minute",
        "agent_burst",
        "instance_max_inflight",
        "max_fanout",
        "retry_after_seconds",
        "upstream_request_timeout_ms",
    ],
)
def test_polarrag_tool_limits_rejects_unbounded_integers(field: str) -> None:
    huge = 10**1000

    with pytest.raises(ValidationError):
        PolarRAGToolLimitsConfig.model_validate({field: huge})

    result = validate_module_config(
        "polarrag_tool_limits",
        {field: huge},
        effective_configs={},
    )
    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_dependency_graph_is_acyclic_and_stable() -> None:
    order = topological_modules(MODULE_REGISTRY)

    assert set(order) == set(MODULE_REGISTRY)
    assert order.index("token_security") < order.index("core_admin")
    assert order.index("token_security") < order.index("user_sso")
    assert "agentic_db_purchase" not in order
    assert "resource_pool" not in order


def test_audit_settings_belong_only_to_observability() -> None:
    sql_schema = MODULE_REGISTRY["sql_security"].model.model_json_schema()
    observability_schema = MODULE_REGISTRY[
        "observability"
    ].model.model_json_schema()

    assert "audit_enabled" not in sql_schema["properties"]
    assert "audit_retention_days" not in sql_schema["properties"]
    assert observability_schema["properties"]["audit_enabled"]["default"] is True
    assert observability_schema["properties"]["audit_retention_days"]["default"] == 180
    assert MODULE_REGISTRY["sql_security"].schema_version == 2
    assert MODULE_REGISTRY["observability"].schema_version == 2


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


def test_token_exchange_only_does_not_require_browser_sso_configuration() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "browser_login_enabled": False,
            "external_token_trust": {
                "enabled": True,
                "provider": "oauth2_introspection",
                "introspection_endpoint": "https://idp.example/introspect",
                "client_id": "pas-token-validator",
                "client_secret": "secret",
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is True
    assert result.normalized_config["browser_login_enabled"] is False


@pytest.mark.parametrize(
    "external_base_url",
    [None, "http://pas.example.com"],
)
def test_token_exchange_only_requires_safe_external_base_url(
    external_base_url: str | None,
) -> None:
    result = validate_module_config(
        "user_sso",
        {
            "browser_login_enabled": False,
            "external_token_trust": {
                "enabled": True,
                "provider": "oauth2_introspection",
                "introspection_endpoint": "https://idp.example/introspect",
                "client_id": "pas-token-validator",
                "client_secret": "secret",
            },
        },
        effective_configs={
            "runtime_policy": {"external_base_url": external_base_url}
        },
    )

    assert result.valid is False
    assert result.error_code == "EXTERNAL_BASE_URL_REQUIRED"


def test_token_exchange_only_requires_external_token_trust() -> None:
    result = validate_module_config(
        "user_sso",
        {"browser_login_enabled": False},
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_token_exchange_only_allows_feishu_identity_context_per_request() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "browser_login_enabled": False,
            "external_token_trust": {
                "enabled": True,
                "provider": "feishu",
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is True
    assert result.normalized_config["external_token_trust"][
        "identity_source_id"
    ] is None


def test_feishu_direct_mcp_requires_static_identity_source() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "browser_login_enabled": False,
            "external_token_trust": {
                "enabled": True,
                "provider": "feishu",
                "direct_mcp_enabled": True,
            },
        },
        effective_configs={"runtime_policy": {"external_base_url": None}},
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


@pytest.mark.parametrize(
    "external_base_url",
    [
        "http://localhost:18760",
        "http://127.0.0.1:18760/",
    ],
)
def test_sso_local_dev_mode_accepts_ipv4_listener_http_origins(
    external_base_url: str,
) -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "http://localhost:19090/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": external_base_url,
            }
        },
        allow_insecure_loopback_urls=True,
    )

    assert result.valid is True


def test_sso_rejects_loopback_http_origin_without_local_dev_mode() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "http://localhost:19090/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "http://127.0.0.1:18760",
            }
        },
    )

    assert result.valid is False
    assert result.error_code == "EXTERNAL_BASE_URL_REQUIRED"


@pytest.mark.parametrize(
    "external_base_url",
    [
        "http://0.0.0.0:18760",
        "http://127.0.0.2:18760",
        "http://[::1]:18760",
        "http://192.168.1.20:18760",
        "http://localhost.example.com:18760",
        "http://localhost:18760/callback-base",
        "http://localhost:18760?tenant=test",
        "http://user@localhost:18760",
    ],
)
def test_sso_local_dev_mode_rejects_non_loopback_or_non_origin_urls(
    external_base_url: str,
) -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "http://localhost:19090/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": external_base_url,
            }
        },
        allow_insecure_loopback_urls=True,
    )

    assert result.valid is False
    assert result.error_code == "EXTERNAL_BASE_URL_REQUIRED"


def test_sso_manual_endpoints_require_explicit_issuer() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "authorization_endpoint": "https://idp.example/authorize",
            "token_endpoint": "https://idp.example/token",
            "client_id": "client",
            "client_secret": "secret",
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_sso_accepts_manual_endpoints_with_explicit_issuer() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "issuer": "https://idp.example/tenant/v2.0",
            "authorization_endpoint": "https://idp.example/authorize",
            "token_endpoint": "https://idp.example/token",
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
    assert result.normalized_config["issuer"] == (
        "https://idp.example/tenant/v2.0"
    )


def test_sso_accepts_standard_token_introspection_trust() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
            "external_token_trust": {
                "enabled": True,
                "provider": "oauth2_introspection",
                "introspection_endpoint": (
                    "https://idp.example/oauth2/introspect"
                ),
                "expected_audience": "pas-mcp",
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is True
    trust = result.normalized_config["external_token_trust"]
    assert trust["provider"] == "oauth2_introspection"
    assert trust["direct_mcp_enabled"] is False
    assert trust["access_token_ttl_seconds"] == 28800


def test_sso_accepts_introspection_userinfo_contract() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "browser-sso-client",
            "client_secret": "browser-sso-secret",
            "external_token_trust": {
                "enabled": True,
                "provider": "oauth2_introspection",
                "introspection_endpoint": (
                    "https://provider.example/oauth2/introspect"
                ),
                "userinfo_endpoint": (
                    "https://provider.example/oauth2/user_info"
                ),
                "client_id": "pas-to-provider",
                "client_secret": "pas-to-provider-secret",
                "identity_source_id": "feishu-source",
                "access_token_ttl_seconds": 86400,
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is True
    trust = result.normalized_config["external_token_trust"]
    assert trust["client_id"] == "pas-to-provider"
    assert trust["userinfo_endpoint"] == (
        "https://provider.example/oauth2/user_info"
    )
    assert trust["access_token_ttl_seconds"] == 86400


def test_sso_introspection_userinfo_requires_verified_identity_source() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "browser-sso-client",
            "client_secret": "browser-sso-secret",
            "external_token_trust": {
                "enabled": True,
                "provider": "oauth2_introspection",
                "introspection_endpoint": (
                    "https://provider.example/oauth2/introspect"
                ),
                "userinfo_endpoint": (
                    "https://provider.example/oauth2/user_info"
                ),
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_sso_rejects_direct_external_tokens_when_trust_is_disabled() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
            "external_token_trust": {
                "enabled": False,
                "direct_mcp_enabled": True,
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_sso_buc_trust_requires_userinfo_form_contract() -> None:
    result = validate_module_config(
        "user_sso",
        {
            "protocol_mode": "oidc",
            "discovery_url": (
                "https://idp.example/.well-known/openid-configuration"
            ),
            "client_id": "client",
            "client_secret": "secret",
            "external_token_trust": {
                "enabled": True,
                "provider": "buc",
            },
        },
        effective_configs={
            "runtime_policy": {
                "external_base_url": "https://agentic.example"
            }
        },
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


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
        SecretFieldSpec("external_token_trust.client_secret"),
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
