from __future__ import annotations

import pytest

from tests._postgres_capacity_harness import (
    HarnessConfigurationError,
    HarnessDisabled,
    generate_schema_name,
    load_harness_config,
    validate_schema_name,
)


def test_harness_is_disabled_before_database_use_without_url():
    with pytest.raises(HarnessDisabled, match="PAS_TEST_POSTGRES_URL"):
        load_harness_config({})


def test_harness_is_disabled_before_database_use_without_explicit_opt_in():
    with pytest.raises(HarnessDisabled, match="PAS_TEST_POSTGRES_SCHEMA_OK=1"):
        load_harness_config(
            {
                "PAS_TEST_POSTGRES_URL": (
                    "postgresql+asyncpg://tester@127.0.0.1/test"
                )
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "mysql+asyncmy://tester@127.0.0.1/test",
        "postgresql://tester@127.0.0.1/test",
        "postgresql+asyncpg://tester@127.0.0.1/",
        (
            "postgresql+asyncpg://tester@127.0.0.1/test"
            "?options=-csearch_path%3Dpublic"
        ),
    ],
)
def test_harness_rejects_unsafe_or_incompatible_urls(url):
    with pytest.raises(HarnessConfigurationError):
        load_harness_config(
            {
                "PAS_TEST_POSTGRES_URL": url,
                "PAS_TEST_POSTGRES_SCHEMA_OK": "1",
            }
        )


@pytest.mark.parametrize(
    "schema_name",
    [
        "public",
        "pas_capacity_test_",
        "pas_capacity_test_deadbeef",
        "pas_capacity_test_" + "0" * 31 + ";",
        "PAS_CAPACITY_TEST_" + "0" * 32,
        "pas_capacity_test_" + "0" * 32 + "_extra",
    ],
)
def test_schema_validator_rejects_unsafe_names(schema_name):
    with pytest.raises(HarnessConfigurationError):
        validate_schema_name(schema_name)


def test_generated_schema_name_is_strictly_valid_and_unique():
    first = generate_schema_name()
    second = generate_schema_name()

    assert validate_schema_name(first) == first
    assert validate_schema_name(second) == second
    assert first != second
