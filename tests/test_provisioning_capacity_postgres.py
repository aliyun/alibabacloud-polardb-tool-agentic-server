from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.core.db_instance_service import (
    CapacityExhausted,
    create_db_instance_resource,
)
from server.models import (
    Agent,
    AgentProvisioningBinding,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    PermissionTemplate,
    PermissionTemplateRevision,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningCapacity,
    User,
)
from server.models.base import utc_now
from tests._postgres_capacity_harness import (
    HarnessDisabled,
    PostgresHarnessConfig,
    load_harness_config,
    run_in_isolated_schema,
    schema_exists,
)


@pytest.fixture(autouse=True)
def encryption_config(monkeypatch):
    from server.config import reset_config

    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_config()
    yield
    reset_config()


def _integration_config() -> PostgresHarnessConfig:
    try:
        return load_harness_config()
    except HarnessDisabled as error:
        pytest.skip(str(error))


@pytest.mark.parametrize("with_template", [False, True], ids=["default-permissions", "template-permissions"])
async def test_postgres_first_capacity_row_race_is_authoritative(with_template):
    config = _integration_config()

    async def exercise(
        factory: async_sessionmaker,
        schema_name: str,
    ) -> None:
        async with factory() as session:
            assert (
                await session.execute(text("SELECT current_schema()"))
            ).scalar_one() == schema_name
            creator = User(external_id="admin", display_name="Admin")
            agent = Agent(name="postgres-agent", max_active_resources=3)
            instance = Instance(
                cluster_id="postgres-capacity",
                name="PostgreSQL Test Backend",
                engine=InstanceEngine.POLARDB_MYSQL,
                topology=InstanceTopology.MULTITENANT,
                status=InstanceStatus.ACTIVE,
                host="backend.internal",
                port=3306,
            )
            session.add_all([creator, agent, instance])
            await session.flush()
            credential = InstanceCredential(
                instance_id=instance.id,
                name="provisioning-admin",
                purpose=CredentialPurpose.PROVISIONING_ADMIN,
                capability=CredentialCapability.ADMIN,
                username_ciphertext="encrypted-user",
                password_ciphertext="encrypted-password",
                created_by_user_id=creator.id,
            )
            session.add(credential)
            await session.flush()
            backend = ProvisioningBackend(
                instance_id=instance.id,
                admin_credential_id=credential.id,
                max_active_resources=2,
            )
            session.add(backend)
            if with_template:
                backend.permission_template_revision = PermissionTemplateRevision(
                    template=PermissionTemplate(name="readonly"), revision=1,
                    privileges_json='["SELECT"]', grant_option=False,
                )
            await session.flush()
            session.add_all(
                [
                    ProvisioningBackendHealth(
                        backend_id=backend.id,
                        healthy=True,
                        checked_at=utc_now(),
                    ),
                    AgentProvisioningBinding(
                        agent_id=agent.id,
                        backend_id=backend.id,
                        created_by_user_id=creator.id,
                    ),
                ]
            )
            await session.commit()
            agent_id = agent.id
            backend_id = backend.id
            instance_id = instance.id
            revision_id = backend.permission_template_revision_id
            assert (
                await session.execute(select(ProvisioningCapacity))
            ).scalars().all() == []

        reservation_started = asyncio.Event()
        reservation_pid = None

        async def attempt(index: int):
            nonlocal reservation_pid
            async with factory() as session:
                if index == -1:
                    reservation_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    await session.commit()
                    reservation_started.set()
                try:
                    return await create_db_instance_resource(
                        session,
                        agent_id=agent_id,
                        client_token=f"postgres-{index}",
                        name=None,
                        db_type="polardb_mysql",
                    )
                except CapacityExhausted:
                    return None

        # Reservation must still lock/recheck the instance after excluding the
        # optional permission revision from FOR UPDATE.
        async with factory() as blocker:
            await blocker.execute(select(Instance.id).where(Instance.id == instance_id).with_for_update())
            blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(attempt(-1))
            try:
                async with asyncio.timeout(10):
                    await reservation_started.wait()
                    while True:
                        if pending.done():
                            # Surface SQL errors rather than waiting for a lock that cannot occur.
                            await pending
                            pytest.fail("reservation did not lock the instance")
                        blockers = await blocker.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": reservation_pid},
                        )
                        if blocker_pid in blockers:
                            break
                        await asyncio.sleep(0.01)
                instance = await blocker.get(Instance, instance_id)
                instance.status = InstanceStatus.STOPPED
                await blocker.commit()
                assert await asyncio.wait_for(pending, timeout=10) is None
                instance.status = InstanceStatus.ACTIVE
                await blocker.commit()
            finally:
                await blocker.rollback()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        results = await asyncio.gather(*(attempt(index) for index in range(8)))
        resources = [result for result in results if result is not None]

        async with factory() as session:
            persisted = (
                await session.execute(select(DBInstanceResource))
            ).scalars().all()
            capacities = (
                await session.execute(select(ProvisioningCapacity))
            ).scalars().all()
        counts = {
            (capacity.scope_type, capacity.scope_id): capacity.active_count
            for capacity in capacities
        }
        assert len(resources) == 2
        assert len(persisted) == 2
        assert counts[("agent", agent_id)] == 2
        assert counts[("backend", backend_id)] == 2
        for resource in persisted:
            snapshot = json.loads(resource.permission_snapshot_json)
            assert snapshot["scope"] == "tenant"
            assert snapshot["grant_option"] is False
            if with_template:
                assert resource.permission_template_revision_id == revision_id
                assert snapshot["revision_id"] == revision_id
                assert snapshot["privileges"] == ["SELECT"]
            else:
                assert resource.permission_template_revision_id is None
                assert snapshot["revision_id"] == "builtin-mysql-default-v1"
                assert "CREATE USER" not in snapshot["privileges"]

    await run_in_isolated_schema(config, exercise)
    assert await schema_exists(config) is False


async def test_postgres_harness_cleans_schema_after_inner_failure():
    config = _integration_config()

    async def fail_after_schema_creation(
        factory: async_sessionmaker,
        schema_name: str,
    ) -> None:
        async with factory() as session:
            assert (
                await session.execute(text("SELECT current_schema()"))
            ).scalar_one() == schema_name
        raise RuntimeError("intentional cleanup probe")

    with pytest.raises(RuntimeError, match="intentional cleanup probe"):
        await run_in_isolated_schema(config, fail_after_schema_creation)

    assert await schema_exists(config) is False
