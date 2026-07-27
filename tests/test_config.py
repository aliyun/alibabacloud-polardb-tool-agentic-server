from __future__ import annotations

import pytest

from server.config import (
    AgenticDBConfig,
    AppConfig,
    PolarDBConfig,
    get_config,
    reset_config,
)


@pytest.fixture(autouse=True)
def clean_runtime_config():
    reset_config()
    yield
    reset_config()


def test_runtime_facade_uses_materialized_safe_defaults():
    config = AppConfig()

    assert config.server.host == "0.0.0.0"
    assert config.server.port == 18760
    assert config.auth.mode == "builtin"
    assert config.sql_security.max_rows == 1000
    assert config.polardb.resource_pool.target_size == 0


def test_get_config_is_stable_without_an_installed_runtime_store():
    first = get_config()
    second = get_config()

    assert first is second


def test_environment_variables_do_not_override_runtime_facade(monkeypatch):
    monkeypatch.setenv("PAS_SERVER_PORT", "9999")
    monkeypatch.setenv("PAS_ALIYUN_REGION_ID", "cn-test")

    config = get_config()

    assert config.server.port == 18760
    assert config.aliyun.region_id == "cn-hangzhou"


def test_provisioning_settings_merge_purchase_spec_and_pool_network():
    config = PolarDBConfig(
        agentic_db={"allow_shut_down": False, "scale_max": 6},
        resource_pool={"target_size": 2, "vpc_id": "vpc-test"},
    )

    settings = config.provisioning_settings()

    assert settings["vpc_id"] == "vpc-test"
    assert settings["allow_shut_down"] == "false"
    assert settings["scale_max"] == "6"
    assert settings["scale_min"] == "0"
    assert settings["storage_type"] == "essdpl1"
    assert settings["db_node_class"] == "polar.mysql.sl.small.c"
    for excluded in (
        "target_size",
        "retry_after_seconds",
        "provisioning_poll_timeout_seconds",
        "endpoint_net_type",
        "enabled",
        "auto_stop_minutes",
        "auto_delete_days",
        "notify_before_delete_days",
    ):
        assert excluded not in settings


def test_agentic_db_spec_settings_exclude_lifecycle_fields():
    spec = AgenticDBConfig(enabled=False).spec_settings()

    assert "enabled" not in spec
    assert spec["db_type"] == "MySQL"
    assert spec["db_minor_version"] == "8.0.2"
    assert spec["proxy_type"] == "GENERAL"
    assert spec["storage_space"] == "20"


def test_tenant_provisioning_rejects_invalid_worker_timing():
    with pytest.raises(ValueError, match="claim renew"):
        AppConfig(
            polardb={
                "tenant_provisioning": {
                    "worker_claim_ttl_seconds": 30,
                    "worker_claim_renew_seconds": 30,
                }
            }
        )
