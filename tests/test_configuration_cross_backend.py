from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.core.config_crypto import ConfigCrypto


ROOT_KEY = b"01234567890123456789012345678901"


@pytest.mark.parametrize(
    ("variable", "dialect", "expected_data_type"),
    (
        ("PAS_TEST_POSTGRES_URL", "postgresql", "text"),
        ("PAS_TEST_MYSQL_URL", "mysql", "longtext"),
    ),
)
async def test_configuration_acceptance_on_external_backend(
    variable: str,
    dialect: str,
    expected_data_type: str,
) -> None:
    url = os.environ.get(variable, "")
    if not url:
        pytest.skip(f"{variable} is not configured")
    if not url.startswith(f"{dialect}+"):
        pytest.fail(f"{variable} must use the {dialect} async dialect")

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ConfigRepository(factory)
    crypto = ConfigCrypto(ROOT_KEY)
    try:
        first, second = await asyncio.gather(
            initialize_configuration(repository, crypto),
            initialize_configuration(repository, crypto),
        )
        assert sum(
            result.bootstrap_token is not None
            for result in (first, second)
        ) == 1

        document = await repository.get_module("user_sso")
        assert document is not None
        large = "跨" * 70_000
        await repository.compare_and_set_module(
            "user_sso",
            expected_revision=document.revision,
            document=document.model_copy(
                update={"draft": {"payload": large}}
            ),
        )
        stored = await repository.get_module("user_sso")
        assert stored is not None
        assert stored.draft == {"payload": large}

        with pytest.raises(ValueError, match="1 MiB"):
            await repository.compare_and_set_module(
                "user_sso",
                expected_revision=stored.revision,
                document=stored.model_copy(
                    update={"draft": {"payload": "x" * 1_048_576}}
                ),
            )

        async with engine.connect() as connection:
            if dialect == "mysql":
                data_type = await connection.scalar(
                    text(
                        "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = 'system_config' "
                        "AND COLUMN_NAME = 'config_value'"
                    )
                )
            else:
                data_type = await connection.scalar(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'system_config' "
                        "AND column_name = 'config_value'"
                    )
                )
        assert data_type == expected_data_type
    finally:
        await engine.dispose()
