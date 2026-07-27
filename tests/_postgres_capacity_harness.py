from __future__ import annotations

import os
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateSchema, DropSchema

from server.models import Base

_SCHEMA_PREFIX = "pas_capacity_test_"
_SCHEMA_RE = re.compile(r"^pas_capacity_test_[0-9a-f]{32}$")
_UNSAFE_QUERY_KEYS = {"options", "server_settings", "search_path"}
_T = TypeVar("_T")


class HarnessDisabled(RuntimeError):
    pass


class HarnessConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PostgresHarnessConfig:
    url: str
    schema_name: str


def validate_schema_name(schema_name: str) -> str:
    if not _SCHEMA_RE.fullmatch(schema_name):
        raise HarnessConfigurationError(
            "PostgreSQL test schema must use the fixed random test prefix"
        )
    return schema_name


def generate_schema_name() -> str:
    return validate_schema_name(f"{_SCHEMA_PREFIX}{secrets.token_hex(16)}")


def load_harness_config(
    environ: Mapping[str, str] | None = None,
) -> PostgresHarnessConfig:
    values = os.environ if environ is None else environ
    url_value = values.get("PAS_TEST_POSTGRES_URL", "").strip()
    if not url_value:
        raise HarnessDisabled("PAS_TEST_POSTGRES_URL is not configured")
    if values.get("PAS_TEST_POSTGRES_SCHEMA_OK") != "1":
        raise HarnessDisabled(
            "PAS_TEST_POSTGRES_SCHEMA_OK=1 is required for isolated schema tests"
        )

    try:
        parsed = make_url(url_value)
    except Exception as error:
        raise HarnessConfigurationError(
            "PAS_TEST_POSTGRES_URL is not a valid SQLAlchemy URL"
        ) from error
    if parsed.drivername != "postgresql+asyncpg":
        raise HarnessConfigurationError(
            "PAS_TEST_POSTGRES_URL must use postgresql+asyncpg"
        )
    if not parsed.database or parsed.database == "/":
        raise HarnessConfigurationError(
            "PAS_TEST_POSTGRES_URL must name an explicit database"
        )
    if _UNSAFE_QUERY_KEYS.intersection(parsed.query):
        raise HarnessConfigurationError(
            "PAS_TEST_POSTGRES_URL cannot override schema or server settings"
        )
    return PostgresHarnessConfig(
        url=url_value,
        schema_name=generate_schema_name(),
    )


def _create_isolated_engine(config: PostgresHarnessConfig):
    schema_name = validate_schema_name(config.schema_name)
    return create_async_engine(
        config.url,
        execution_options={
            "schema_translate_map": {None: schema_name},
        },
        connect_args={
            "server_settings": {
                "search_path": schema_name,
                "application_name": "pas_capacity_isolated_test",
            }
        },
    )


async def run_in_isolated_schema(
    config: PostgresHarnessConfig,
    operation: Callable[
        [async_sessionmaker[AsyncSession], str],
        Awaitable[_T],
    ],
) -> _T:
    schema_name = validate_schema_name(config.schema_name)
    engine = _create_isolated_engine(config)
    schema_created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema_name))
            schema_created = True
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return await operation(factory, schema_name)
    finally:
        try:
            if schema_created:
                async with engine.begin() as connection:
                    await connection.execute(
                        DropSchema(
                            validate_schema_name(schema_name),
                            cascade=True,
                            if_exists=True,
                        )
                    )
        finally:
            await engine.dispose()


async def schema_exists(config: PostgresHarnessConfig) -> bool:
    schema_name = validate_schema_name(config.schema_name)
    engine = create_async_engine(config.url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT 1 FROM pg_catalog.pg_namespace "
                    "WHERE nspname = :schema_name"
                ),
                {"schema_name": schema_name},
            )
            return result.scalar_one_or_none() is not None
    finally:
        await engine.dispose()
