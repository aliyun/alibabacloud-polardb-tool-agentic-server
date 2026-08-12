from __future__ import annotations

from server.config import PolarDBConfig
from server.configuration.runtime import RuntimeConfigStore
from server.configuration.types import (
    EffectiveConfig,
    ModuleDocument,
    ModuleState,
)
from server.models import SystemConfig
from tests._configuration_helpers import create_config_context


async def test_worker_and_pool_settings_project_from_runtime_policy() -> None:
    context = await create_config_context()
    try:
        runtime = await context.repository.get_module("runtime_policy")
        runtime.effective.config.update(
            {
                "worker_poll_interval_seconds": 3,
                "worker_claim_ttl_seconds": 180,
                "worker_claim_renew_seconds": 45,
                "max_connections_per_pool": 11,
                "delete_cooldown_duration_hours": 36,
                "dedicated_pool_preparation_mode": "openapi_only",
            }
        )
        runtime.effective.revision += 1
        await context.repository.compare_and_set_module(
            "runtime_policy",
            expected_revision=runtime.revision,
            document=runtime,
        )
        store = RuntimeConfigStore(context.repository, context.crypto)
        await store.poll_once()
        config = store.current()

        assert (
            config.polardb.tenant_provisioning.worker_poll_interval_seconds
            == 3
        )
        assert (
            config.polardb.tenant_provisioning.worker_claim_ttl_seconds
            == 180
        )
        assert (
            config.polardb.connection_pool.max_connections_per_pool
            == 11
        )
        assert (
            config.polardb.tenant_provisioning
            .delete_cooldown_duration_hours
            == 36
        )
        assert (
            config.polardb.tenant_provisioning
            .dedicated_pool_preparation_mode
            == "openapi_only"
        )
    finally:
        await context.close()


async def test_historical_agentic_purchase_row_is_inert_and_preserved() -> None:
    context = await create_config_context()
    try:
        historical = ModuleDocument(
            revision=7,
            workflow_state=ModuleState.ACTIVE,
            initial_state=ModuleState.SKIPPED,
            desired_state=ModuleState.ACTIVE,
            effective=EffectiveConfig(
                revision=7,
                state=ModuleState.ACTIVE,
                config={"db_node_class": "legacy.must.not.apply"},
            ),
        )
        async with context.repository.session_factory() as session:
            async with session.begin():
                setup = await session.get(SystemConfig, "setup.status")
                assert setup is not None
                setup.config_version += 1
                row = await session.get(
                    SystemConfig, "module.agentic_db_purchase"
                )
                if row is None:
                    row = SystemConfig(
                        config_key="module.agentic_db_purchase",
                        config_value=historical.model_dump_json(
                            exclude_none=True
                        ),
                        config_version=setup.config_version,
                    )
                    session.add(row)
                else:
                    row.config_value = historical.model_dump_json(
                        exclude_none=True
                    )
                    row.config_version = setup.config_version

        store = RuntimeConfigStore(context.repository, context.crypto)
        await store.poll_once()

        assert "agentic_db" not in PolarDBConfig.model_fields
        assert await context.repository.get_config_row(
            "module.agentic_db_purchase"
        ) is not None
    finally:
        await context.close()
