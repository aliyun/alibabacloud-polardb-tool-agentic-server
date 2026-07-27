from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.repository import (
    ConfigConflict,
    ConfigRepository,
)
from server.configuration.types import ModuleDocument, ModuleState
from server.models import Base


@pytest.fixture
async def repository() -> ConfigRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    value = ConfigRepository(factory)
    await value.ensure_setup_status()
    yield value
    await engine.dispose()


def module_document(value: str) -> ModuleDocument:
    return ModuleDocument(
        revision=0,
        workflow_state=ModuleState.DRAFT,
        initial_state=ModuleState.SKIPPED,
        draft={"value": value},
    )


async def test_mutation_increments_one_global_version(
    repository: ConfigRepository,
) -> None:
    before = await repository.global_version()

    updated = await repository.compare_and_set_module(
        "user_sso",
        expected_revision=0,
        document=module_document("first"),
    )

    assert updated.config_version == before + 1
    assert await repository.global_version() == before + 1
    stored = json.loads(updated.config_value)
    assert stored["revision"] == 1


async def test_stale_revision_does_not_overwrite(
    repository: ConfigRepository,
) -> None:
    await repository.compare_and_set_module(
        "user_sso",
        expected_revision=0,
        document=module_document("first"),
    )

    with pytest.raises(ConfigConflict):
        await repository.compare_and_set_module(
            "user_sso",
            expected_revision=0,
            document=module_document("second"),
        )

    stored = await repository.get_module("user_sso")
    assert stored is not None
    assert stored.draft == {"value": "first"}


async def test_oversized_document_is_rejected(
    repository: ConfigRepository,
) -> None:
    oversized = module_document("x" * 1_048_576)

    with pytest.raises(ValueError, match="1 MiB"):
        await repository.compare_and_set_module(
            "user_sso",
            expected_revision=0,
            document=oversized,
        )

