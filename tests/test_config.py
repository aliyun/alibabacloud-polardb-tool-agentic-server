from __future__ import annotations

import pytest

from server.config import (
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
    assert "resource_pool" not in PolarDBConfig.model_fields
    assert "agentic_db" not in PolarDBConfig.model_fields


def test_aliyun_runtime_config_keeps_only_the_active_mode_credentials():
    config = AppConfig(
        aliyun={
            "credential_mode": "assume_role",
            "assume_role": {
                "source_access_key_id": "source-ak",
                "source_access_key_secret": "source-sk",
                "role_arn": "acs:ram::1234567890123456:role/pas-runtime",
                "role_session_name": "polardb-agentic-a1b2c3d4",
                "duration_seconds": 3600,
            },
            "credential_digest": "keyed-digest",
            "config_revision": 4,
        }
    )

    assert config.aliyun.assume_role is not None
    assert config.aliyun.direct_ak is None
    assert config.aliyun.credential_digest == "keyed-digest"
    assert config.aliyun.config_revision == 4
    assert config.aliyun.has_active_credentials()


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
