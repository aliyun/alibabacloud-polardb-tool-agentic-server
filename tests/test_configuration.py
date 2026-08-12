from __future__ import annotations

import pytest

from server.config import TenantProvisioningConfig
from server.configuration.registry import validate_module_config


def test_delete_cooldown_defaults_to_24_hours():
    assert TenantProvisioningConfig().delete_cooldown_duration_hours == 24

    result = validate_module_config(
        "runtime_policy",
        {},
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["delete_cooldown_duration_hours"] == 24


def test_dedicated_simulation_is_explicit_and_disabled_by_default():
    assert TenantProvisioningConfig().dedicated_pool_simulation_enabled is False

    result = validate_module_config(
        "runtime_policy",
        {"dedicated_pool_simulation_enabled": True},
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["dedicated_pool_simulation_enabled"] is True


def test_dedicated_preparation_defaults_to_full_and_accepts_openapi_only():
    assert TenantProvisioningConfig().dedicated_pool_preparation_mode == "full"

    result = validate_module_config(
        "runtime_policy",
        {"dedicated_pool_preparation_mode": "openapi_only"},
        effective_configs={},
    )

    assert result.valid is True
    assert result.normalized_config["dedicated_pool_preparation_mode"] == (
        "openapi_only"
    )


def test_dedicated_preparation_rejects_unknown_mode():
    result = validate_module_config(
        "runtime_policy",
        {"dedicated_pool_preparation_mode": "automatic"},
        effective_configs={},
    )

    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


@pytest.mark.parametrize("value", [0, -1])
def test_delete_cooldown_rejects_immediate_or_negative_cleanup(value: int):
    with pytest.raises(ValueError):
        TenantProvisioningConfig(
            delete_cooldown_duration_hours=value
        )

    result = validate_module_config(
        "runtime_policy",
        {"delete_cooldown_duration_hours": value},
        effective_configs={},
    )
    assert result.valid is False
    assert result.error_code == "INVALID_MODULE_CONFIG"


def test_effective_delete_cooldown_uses_most_specific_override():
    from server.core.permission_template_service import (
        effective_delete_cooldown_hours,
    )

    assert effective_delete_cooldown_hours(
        resource_override=6,
        pool_or_backend_override=12,
        global_default=24,
    ) == 6
    assert effective_delete_cooldown_hours(
        resource_override=None,
        pool_or_backend_override=12,
        global_default=24,
    ) == 12
    assert effective_delete_cooldown_hours(
        resource_override=None,
        pool_or_backend_override=None,
        global_default=24,
    ) == 24


@pytest.mark.parametrize(
    "values",
    [
        {"resource_override": 0, "pool_or_backend_override": None, "global_default": 24},
        {"resource_override": None, "pool_or_backend_override": 0, "global_default": 24},
        {"resource_override": None, "pool_or_backend_override": None, "global_default": 0},
    ],
)
def test_effective_delete_cooldown_rejects_nonpositive_selected_value(values):
    from server.core.permission_template_service import (
        effective_delete_cooldown_hours,
    )

    with pytest.raises(ValueError, match="at least 1 hour"):
        effective_delete_cooldown_hours(**values)
