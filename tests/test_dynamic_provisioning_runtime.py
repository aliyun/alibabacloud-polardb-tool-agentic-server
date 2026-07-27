from __future__ import annotations

from server.configuration.runtime import RuntimeConfigStore
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
    finally:
        await context.close()
