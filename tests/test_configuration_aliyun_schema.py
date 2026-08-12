from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from server.configuration.aliyun_access import (
    AliyunAccessConfig,
    AliyunAccessMutation,
    generate_role_session_name,
)


def _assume_role_config() -> dict[str, object]:
    return {
        "credential_mode": "assume_role",
        "region_id": "cn-hangzhou",
        "openapi_network": "public",
        "assume_role": {
            "source_access_key_id": "LTAI-example",
            "source_access_key_secret": "secret",
            "role_arn": "acs:ram::1234567890123456:role/pas-runtime",
            "role_session_name": "polardb-agentic-a1b2c3d4",
            "duration_seconds": 3600,
        },
    }


def test_assume_role_accepts_source_keys_and_role_arn() -> None:
    value = AliyunAccessConfig.model_validate(
        _assume_role_config()
    )

    assert value.direct_ak is None
    assert value.assume_role is not None
    assert value.assume_role.duration_seconds == 3600


@pytest.mark.parametrize(
    "missing_field",
    [
        "source_access_key_id",
        "source_access_key_secret",
        "role_arn",
    ],
)
def test_assume_role_requires_source_keys_and_role_arn(
    missing_field: str,
) -> None:
    config = _assume_role_config()
    assume_role = config["assume_role"]
    assert isinstance(assume_role, dict)
    assume_role.pop(missing_field)

    with pytest.raises(ValidationError):
        AliyunAccessConfig.model_validate(config)


def test_ecs_mode_rejects_access_keys() -> None:
    with pytest.raises(ValidationError):
        AliyunAccessConfig.model_validate(
            {
                "credential_mode": "ecs_ram_role",
                "ecs_ram_role": {"role_name": "pas-runtime"},
                "direct_ak": {
                    "access_key_id": "ak",
                    "access_key_secret": "sk",
                },
            }
        )


def test_selected_mode_requires_its_configuration_block() -> None:
    with pytest.raises(ValidationError):
        AliyunAccessConfig.model_validate({"credential_mode": "direct_ak"})


def test_mutation_requires_selected_block_by_default() -> None:
    with pytest.raises(ValidationError):
        AliyunAccessMutation.model_validate({"credential_mode": "direct_ak"})


def test_mutation_allows_reusing_the_selected_retained_block() -> None:
    mutation = AliyunAccessMutation.model_validate(
        {
            "credential_mode": "direct_ak",
            "transition": {"selected_mode_action": "reuse_retained"},
        }
    )

    assert mutation.direct_ak is None


def test_mutation_ecs_mode_rejects_access_key_blocks() -> None:
    with pytest.raises(ValidationError):
        AliyunAccessMutation.model_validate(
            {
                "credential_mode": "ecs_ram_role",
                "ecs_ram_role": {"role_name": "pas-runtime"},
                "direct_ak": {
                    "access_key_id": "ak",
                    "access_key_secret": "sk",
                },
            }
        )


def test_mutation_allows_valid_retained_direct_ak_for_assume_role() -> None:
    config = _assume_role_config()
    config["direct_ak"] = {
        "access_key_id": "retained-ak",
        "access_key_secret": "retained-sk",
    }

    mutation = AliyunAccessMutation.model_validate(config)

    assert mutation.direct_ak is not None


def test_valid_non_selected_block_is_retained() -> None:
    value = AliyunAccessConfig.model_validate(
        {
            "credential_mode": "assume_role",
            "direct_ak": {
                "access_key_id": "retained-ak",
                "access_key_secret": "retained-sk",
            },
            "assume_role": {
                "source_access_key_id": "source-ak",
                "source_access_key_secret": "source-sk",
                "role_arn": "acs:ram::1234567890123456:role/pas-runtime",
                "role_session_name": "polardb-agentic-a1b2c3d4",
            },
        }
    )

    assert value.direct_ak is not None
    assert value.credential_mode == "assume_role"


def test_generated_role_session_name_has_required_prefix_and_suffix() -> None:
    assert re.fullmatch(
        r"polardb-agentic-[0-9a-f]{8}",
        generate_role_session_name(),
    )
