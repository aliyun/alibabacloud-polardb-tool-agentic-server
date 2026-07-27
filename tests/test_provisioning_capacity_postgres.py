from __future__ import annotations

import asyncio
import base64
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


async def test_postgres_first_capacity_row_race_is_authoritative():
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
            assert (
                await session.execute(select(ProvisioningCapacity))
            ).scalars().all() == []

        async def attempt(index: int):
            async with factory() as session:
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
