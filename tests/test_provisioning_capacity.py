from __future__ import annotations

import asyncio
import base64
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import AppConfig, TenantProvisioningConfig, reset_config
from server.core.db_instance_service import (
    CapacityExhausted,
    create_db_instance_resource,
)
from server.core.provisioning_capacity import (
    CapacityUnavailable,
    _capacity_insert_statement,
    _capacity_lock_statement,
    reserve_capacity_and_insert,
)
from server.models import (
    Agent,
    AgentProvisioningBinding,
    AgentStatus,
    Base,
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


@pytest.fixture(autouse=True)
def encryption_config(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_config()
    yield
    reset_config()


async def _seed(
    factory: async_sessionmaker,
    *,
    backend_limits: tuple[int, ...],
    agent_limit: int,
) -> tuple[str, list[str]]:
    async with factory() as session:
        creator = User(external_id="admin", display_name="Admin")
        agent = Agent(name="capacity-agent", max_active_resources=agent_limit)
        session.add_all([creator, agent])
        await session.flush()
        backend_ids: list[str] = []
        for index, limit in enumerate(backend_limits):
            instance = Instance(
                cluster_id=f"cluster-{index}",
                name=f"Backend {index}",
                engine=InstanceEngine.POLARDB_MYSQL,
                topology=InstanceTopology.MULTITENANT,
                status=InstanceStatus.ACTIVE,
                host=f"backend-{index}.internal",
                port=3306,
            )
            session.add(instance)
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
                priority=len(backend_limits) - index,
                max_active_resources=limit,
            )
            session.add(backend)
            await session.flush()
            backend_ids.append(backend.id)
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
        return agent.id, backend_ids


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'capacity.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_capacity_lock_is_authoritative_under_concurrency(database):
    agent_id, _ = await _seed(database, backend_limits=(2,), agent_limit=10)

    async def attempt(index: int):
        async with database() as session:
            return await create_db_instance_resource(
                session,
                agent_id=agent_id,
                client_token=f"token-{index}",
                name=None,
                db_type="polardb_mysql",
            )

    results = await asyncio.gather(
        *(attempt(index) for index in range(3)),
        return_exceptions=True,
    )

    assert sum(isinstance(item, DBInstanceResource) for item in results) == 2
    assert sum(isinstance(item, CapacityExhausted) for item in results) == 1


async def test_capacity_retries_next_candidate(database):
    agent_id, backend_ids = await _seed(
        database,
        backend_limits=(1, 1),
        agent_limit=10,
    )
    async with database() as session:
        session.add(
            ProvisioningCapacity(
                scope_type="backend",
                scope_id=backend_ids[0],
                active_count=1,
            )
        )
        await session.commit()
        resource = await create_db_instance_resource(
            session,
            agent_id=agent_id,
            client_token="retry-next",
            name=None,
            db_type="polardb_mysql",
        )

    assert resource.backend_id == backend_ids[1]


async def test_capacity_rechecks_binding_before_reserving(database):
    agent_id, backend_ids = await _seed(
        database,
        backend_limits=(1,),
        agent_limit=10,
    )
    async with database() as session:
        binding = (
            await session.execute(select(AgentProvisioningBinding))
        ).scalar_one()
        binding.enabled = False
        await session.commit()

        with pytest.raises(CapacityUnavailable):
            await reserve_capacity_and_insert(
                session,
                agent_id=agent_id,
                engine=InstanceEngine.POLARDB_MYSQL,
                candidate_ids=backend_ids,
                build_resource=lambda backend_id: DBInstanceResource(
                    owner_agent_id=agent_id,
                    backend_id=backend_id,
                    client_token="stale-selection",
                    request_fingerprint="0" * 64,
                ),
            )


async def test_capacity_rechecks_engine_and_retries_next_candidate(database):
    agent_id, backend_ids = await _seed(
        database,
        backend_limits=(1, 1),
        agent_limit=10,
    )
    async with database() as session:
        first_backend = await session.get(ProvisioningBackend, backend_ids[0])
        await session.execute(
            text("UPDATE instances SET engine = 'redis' WHERE id = :instance_id"),
            {"instance_id": first_backend.instance_id},
        )
        await session.commit()

        resource = await reserve_capacity_and_insert(
            session,
            agent_id=agent_id,
            engine=InstanceEngine.POLARDB_MYSQL,
            candidate_ids=backend_ids,
            build_resource=lambda backend_id: DBInstanceResource(
                owner_agent_id=agent_id,
                backend_id=backend_id,
                client_token="stale-engine",
                request_fingerprint="0" * 64,
            ),
        )

    assert resource.backend_id == backend_ids[1]
    async with database() as session:
        first_capacity = (
            await session.execute(
                select(ProvisioningCapacity).where(
                    ProvisioningCapacity.scope_type == "backend",
                    ProvisioningCapacity.scope_id == backend_ids[0],
                )
            )
        ).scalar_one_or_none()
    assert first_capacity is None


async def test_disabled_agent_fails_closed(database):
    agent_id, _ = await _seed(database, backend_limits=(2,), agent_limit=2)
    async with database() as session:
        agent = await session.get(Agent, agent_id)
        agent.status = AgentStatus.DISABLED
        await session.commit()
        with pytest.raises(CapacityExhausted):
            await create_db_instance_resource(
                session,
                agent_id=agent_id,
                client_token="disabled",
                name=None,
                db_type="polardb_mysql",
            )


async def test_nullable_agent_limit_uses_resource_named_fallback(
    database,
):
    from server import config as config_module

    config_module._config = AppConfig(
        polardb={
            "tenant_provisioning": {
                "max_active_resources_per_agent": 1
            }
        }
    )
    agent_id, _ = await _seed(database, backend_limits=(2,), agent_limit=2)
    async with database() as session:
        agent = await session.get(Agent, agent_id)
        agent.max_active_resources = None
        await session.commit()
        await create_db_instance_resource(
            session,
            agent_id=agent_id,
            client_token="first",
            name=None,
            db_type="polardb_mysql",
        )
        with pytest.raises(CapacityExhausted):
            await create_db_instance_resource(
                session,
                agent_id=agent_id,
                client_token="second",
                name=None,
                db_type="polardb_mysql",
            )
    reset_config()


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        (sqlite.dialect(), "ON CONFLICT"),
        (postgresql.dialect(), "ON CONFLICT"),
        (mysql.dialect(), "INSERT IGNORE"),
    ],
)
def test_capacity_insert_compiles_for_supported_dialects(dialect, expected):
    statement = _capacity_insert_statement(
        dialect.name,
        scope_type="agent",
        scope_id="agent-1",
    )
    assert expected in str(statement.compile(dialect=dialect)).upper()


@pytest.mark.parametrize(
    ("dialect", "has_for_update"),
    [("sqlite", False), ("postgresql", True), ("mysql", True)],
)
def test_capacity_lock_statement_matches_dialect(dialect, has_for_update):
    statement = _capacity_lock_statement(
        dialect,
        agent_id="agent-1",
        backend_id="backend-1",
    )
    compiled_dialect = {
        "sqlite": sqlite.dialect(),
        "postgresql": postgresql.dialect(),
        "mysql": mysql.dialect(),
    }[dialect]
    sql = str(statement.compile(dialect=compiled_dialect)).upper()
    assert ("FOR UPDATE" in sql) is has_for_update


def test_resource_named_runtime_settings_fall_back_to_legacy_names():
    legacy = TenantProvisioningConfig(
        max_active_leases_per_agent=7,
        health_stale_after_seconds=40,
    )
    renamed = TenantProvisioningConfig(
        max_active_leases_per_agent=7,
        max_active_resources_per_agent=3,
        health_stale_after_seconds=40,
        backend_health_stale_after_seconds=20,
    )

    assert legacy.effective_max_active_resources_per_agent == 7
    assert legacy.effective_backend_health_stale_after_seconds == 40
    assert renamed.effective_max_active_resources_per_agent == 3
    assert renamed.effective_backend_health_stale_after_seconds == 20


async def test_global_agent_capacity_applies_across_backends(database):
    agent_id, _ = await _seed(database, backend_limits=(2, 2), agent_limit=1)
    async with database() as session:
        first = await create_db_instance_resource(
            session,
            agent_id=agent_id,
            client_token="first",
            name=None,
            db_type="polardb_mysql",
        )
        assert first.id
    async with database() as session:
        with pytest.raises(CapacityExhausted):
            await create_db_instance_resource(
                session,
                agent_id=agent_id,
                client_token="second",
                name=None,
                db_type="polardb_mysql",
            )

    async with database() as session:
        capacities = (
            await session.execute(
                select(ProvisioningCapacity).order_by(
                    ProvisioningCapacity.scope_type,
                    ProvisioningCapacity.scope_id,
                )
            )
        ).scalars().all()
    agent_rows = [
        row for row in capacities if row.scope_type == "agent"
    ]
    assert len(agent_rows) == 1
    assert agent_rows[0].active_count == 1


async def test_concurrency_stress_never_exceeds_capacity(database):
    agent_id, _ = await _seed(database, backend_limits=(4, 4), agent_limit=5)

    async def attempt(index: int):
        async with database() as session:
            try:
                return await create_db_instance_resource(
                    session,
                    agent_id=agent_id,
                    client_token=f"stress-{index}",
                    name=None,
                    db_type="polardb_mysql",
                )
            except CapacityExhausted:
                return None

    results = await asyncio.gather(*(attempt(index) for index in range(20)))
    resources = [item for item in results if item is not None]

    assert len(resources) == 5
    async with database() as session:
        agent_capacity = (
            await session.execute(
                select(ProvisioningCapacity).where(
                    ProvisioningCapacity.scope_type == "agent",
                    ProvisioningCapacity.scope_id == agent_id,
                )
            )
        ).scalar_one()
        persisted = (
            await session.execute(select(DBInstanceResource))
        ).scalars().all()
    assert agent_capacity.active_count == 5
    assert len(persisted) == 5
