"""Opt-in acceptance on migrated disposable MySQL/PostgreSQL test databases."""

import asyncio
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.core.config_crypto import ConfigCrypto
from server.db.migration_runner import migration_lock, run_migration
from server.db.schema import DatabaseSchemaError
from server.features.knowledge import KnowledgeRuntime, KnowledgeState, KnowledgeUnavailable
from tests._configuration_helpers import ROOT_KEY
from tests.test_knowledge_feature import set_enabled


@pytest.mark.parametrize("variable", ["PAS_TEST_MYSQL_URL", "PAS_TEST_POSTGRES_URL"])
async def test_database_lock_and_concurrent_knowledge_admission(variable):
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"{variable} is not configured")
    engine = create_async_engine(url)
    repository = ConfigRepository(async_sessionmaker(engine, expire_on_commit=False))
    try:
        async with engine.connect() as connection:
            async with migration_lock(connection, url):
                with pytest.raises(DatabaseSchemaError) as error:
                    await run_migration(url)
                assert error.value.code == "DATABASE_MIGRATION_BUSY"
        await initialize_configuration(repository, ConfigCrypto(ROOT_KEY))
        revision = await set_enabled(SimpleNamespace(repository=repository), True)
        state = KnowledgeState(repository)
        feature = KnowledgeRuntime(state, managed=False)
        await feature.start()
        await state.cancel_drain(revision)
        release = asyncio.Event()
        entered = asyncio.Queue()

        async def work():
            async with feature.operation("backend-concurrency-test"):
                await entered.put(True)
                await release.wait()

        tasks = [asyncio.create_task(work()) for _ in range(6)]
        try:
            for _ in tasks:
                await asyncio.wait_for(entered.get(), timeout=10)
            assert (await state.disable_blockers())["in_flight"] == 6
            await state.begin_drain()
            with pytest.raises(KnowledgeUnavailable):
                async with feature.operation("must-reject"):
                    pass
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert (await state.disable_blockers())["in_flight"] == 0
        await state.cancel_drain(revision)
        await feature.heartbeat(stopped=True)
    finally:
        await engine.dispose()
