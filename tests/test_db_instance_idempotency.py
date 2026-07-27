from __future__ import annotations

import asyncio
import base64
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from server.config import reset_config
from server.core.crypto import decrypt
from server.core.db_instance_service import (
    IdempotencyConflict,
    InvalidClientToken,
    UnsupportedDBType,
    create_db_instance_resource,
    delete_db_instance_resource,
    normalize_resource_name,
    request_fingerprint,
)
from server.core.resource_write_guard import (
    ResourceSessionNotIdle,
    serialized_resource_write,
)
from server.models import (
    Agent,
    AgentProvisioningBinding,
    Base,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    DBInstanceStatus,
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


@pytest.mark.parametrize(
    ("name", "normalized"),
    [("  Orders  ", "Orders"), ("e\u0301", "é"), (None, None)],
)
def test_name_normalization(name, normalized):
    assert normalize_resource_name(name) == normalized


@pytest.mark.parametrize("name", ["", "   ", "has\nnewline", "x" * 129])
def test_name_normalization_rejects_invalid_display_names(name):
    with pytest.raises(ValueError):
        normalize_resource_name(name)


def test_request_fingerprint_has_stable_version_one_golden_values():
    assert request_fingerprint("polardb_mysql", "  Orders  ") == (
        "7a4eb1aaf94e7e85f48287e7c1d19c8ced99390da1b6307a05a43d1916600db4"
    )
    assert request_fingerprint("polardb_mysql", None) == (
        "bc55b3b4961681a71b8622e436a2a30f53c6b7aa801bbd5753c720171fc4210a"
    )
    with pytest.raises(ValueError):
        request_fingerprint("polardb_mysql", None, version=2)


async def _seed_backend(
    session: AsyncSession,
    *,
    agent_name: str = "idempotency-agent",
    backend_limit: int = 10,
) -> tuple[Agent, ProvisioningBackend]:
    creator = User(
        external_id=f"{agent_name}-admin",
        display_name="Admin",
    )
    agent = Agent(name=agent_name, max_active_resources=10)
    instance = Instance(
        cluster_id=f"{agent_name}-cluster",
        name="Multitenant Backend",
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
        username_ciphertext="encrypted-admin",
        password_ciphertext="encrypted-password",
        created_by_user_id=creator.id,
    )
    session.add(credential)
    await session.flush()
    backend = ProvisioningBackend(
        instance_id=instance.id,
        admin_credential_id=credential.id,
        priority=1,
        max_active_resources=backend_limit,
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
    return agent, backend


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


async def test_create_persists_normalized_name_and_encrypted_resource_credential(
    session,
):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id

    resource = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="  Orders  ",
        db_type="polardb_mysql",
    )

    credential = (
        await session.execute(
            select(InstanceCredential).where(
                InstanceCredential.resource_id == resource.id
            )
        )
    ).scalar_one()
    assert resource.name == "Orders"
    assert resource.tenant_name is not None
    assert resource.name not in {
        resource.tenant_name,
        resource.resource_config_name,
        resource.database_name,
    }
    assert credential.purpose == CredentialPurpose.RESOURCE_ACCESS
    assert credential.capability == CredentialCapability.READWRITE
    assert credential.version == 1
    assert credential.username_ciphertext != credential.database_name
    assert credential.password_ciphertext is not None
    assert decrypt(credential.username_ciphertext) == credential.database_name
    assert decrypt(credential.password_ciphertext)


async def test_resource_write_guard_never_silently_rolls_back_read_transaction(
    session,
):
    await session.execute(select(Agent.id))
    assert session.in_transaction()

    with pytest.raises(RuntimeError, match="idle session"):
        async with serialized_resource_write(session):
            pytest.fail("guard must reject a caller-owned transaction")

    assert session.in_transaction()
    await session.rollback()


async def test_create_rejects_pending_mutation_without_rolling_it_back(
    session,
):
    pending = Agent(name="pending-agent")
    session.add(pending)
    await session.flush()
    pending_id = pending.id

    with pytest.raises(ResourceSessionNotIdle) as error:
        await create_db_instance_resource(
            session,
            agent_id=pending_id,
            client_token="must-not-touch-caller-transaction",
            name=None,
            db_type="polardb_mysql",
        )

    assert error.value.code == "RESOURCE_SESSION_NOT_IDLE"
    assert session.in_transaction()
    assert pending in session
    assert pending.id == pending_id
    await session.rollback()
    assert (
        await session.execute(
            select(Agent.id).where(Agent.id == pending_id)
        )
    ).scalar_one_or_none() is None


async def test_create_rejects_explicit_transaction_without_closing_it(
    session,
):
    async with session.begin():
        assert session.in_transaction()
        with pytest.raises(ResourceSessionNotIdle):
            await create_db_instance_resource(
                session,
                agent_id="agent-id",
                client_token="explicit-transaction",
                name=None,
                db_type="polardb_mysql",
            )
        assert session.in_transaction()


async def test_same_input_returns_original_without_incrementing_capacity(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    first = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="orders",
        db_type="polardb_mysql",
    )
    second = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="orders",
        db_type="polardb_mysql",
    )

    assert second.id == first.id
    counts = (
        await session.execute(
            select(
                ProvisioningCapacity.scope_type,
                ProvisioningCapacity.active_count,
            )
        )
    ).all()
    assert sorted(counts) == [("agent", 1), ("backend", 1)]


async def test_same_input_replay_runs_required_before_commit_once_per_call(
    session,
):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    audited_resource_ids: list[str] = []

    async def audit_before_commit(_session, resource):
        audited_resource_ids.append(resource.id)

    first = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-audited",
        name="orders",
        db_type="polardb_mysql",
        before_commit=audit_before_commit,
    )
    replay = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-audited",
        name="orders",
        db_type="polardb_mysql",
        before_commit=audit_before_commit,
    )

    assert replay.id == first.id
    assert audited_resource_ids == [first.id, first.id]
    capacities = (
        await session.execute(
            select(
                ProvisioningCapacity.scope_type,
                ProvisioningCapacity.active_count,
            )
        )
    ).all()
    assert sorted(capacities) == [("agent", 1), ("backend", 1)]


async def test_same_token_with_different_input_conflicts(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="orders",
        db_type="polardb_mysql",
    )

    with pytest.raises(IdempotencyConflict):
        await create_db_instance_resource(
            session,
            agent_id=agent_id,
            client_token="deploy-1",
            name="customers",
            db_type="polardb_mysql",
        )


async def test_deleted_resource_permanently_consumes_client_token(session):
    agent, _ = await _seed_backend(session)
    agent_id = agent.id
    first = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="orders",
        db_type="polardb_mysql",
    )
    first.status = DBInstanceStatus.DELETED
    await session.commit()

    second = await create_db_instance_resource(
        session,
        agent_id=agent_id,
        client_token="deploy-1",
        name="orders",
        db_type="polardb_mysql",
    )

    assert second.id == first.id
    assert second.status == DBInstanceStatus.DELETED


@pytest.mark.parametrize(
    "client_token",
    ["", "space token", "slash/token", "x" * 129],
)
async def test_invalid_client_token_is_rejected_before_database_work(
    session,
    client_token,
):
    with pytest.raises(InvalidClientToken):
        await create_db_instance_resource(
            session,
            agent_id="agent-does-not-matter",
            client_token=client_token,
            name=None,
            db_type="polardb_mysql",
        )


async def test_unsupported_database_type_is_rejected(session):
    with pytest.raises(UnsupportedDBType):
        await create_db_instance_resource(
            session,
            agent_id="agent-does-not-matter",
            client_token="deploy-1",
            name=None,
            db_type="postgresql",
        )


async def test_missing_encryption_key_rolls_back_resource_and_capacity(
    session,
    monkeypatch,
):
    agent, _ = await _seed_backend(session)
    monkeypatch.delenv("PAS_ENCRYPTION_KEY")
    reset_config()

    with pytest.raises(ValueError, match="PAS_ENCRYPTION_KEY is required"):
        await create_db_instance_resource(
            session,
            agent_id=agent.id,
            client_token="deploy-without-key",
            name="orders",
            db_type="polardb_mysql",
        )

    resource_count = (
        await session.execute(
            select(func.count()).select_from(DBInstanceResource)
        )
    ).scalar_one()
    capacity_count = (
        await session.execute(
            select(func.count()).select_from(ProvisioningCapacity)
        )
    ).scalar_one()
    assert resource_count == 0
    assert capacity_count == 0


async def test_concurrent_same_token_creates_one_resource_and_reserves_once(
    tmp_path,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'idempotency.db'}"
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as seed_session:
        agent, _ = await _seed_backend(seed_session, agent_name="concurrent-agent")
        agent_id = agent.id

    async def create_once():
        async with factory() as create_session:
            return await create_db_instance_resource(
                create_session,
                agent_id=agent_id,
                client_token="same-token",
                name="orders",
                db_type="polardb_mysql",
            )

    first, second = await asyncio.gather(create_once(), create_once())

    async with factory() as verify_session:
        resource_count = (
            await verify_session.execute(
                select(func.count()).select_from(DBInstanceResource)
            )
        ).scalar_one()
        credential_count = (
            await verify_session.execute(
                select(func.count())
                .select_from(InstanceCredential)
                .where(InstanceCredential.resource_id.is_not(None))
            )
        ).scalar_one()
        capacities = (
            await verify_session.execute(
                select(
                    ProvisioningCapacity.scope_type,
                    ProvisioningCapacity.active_count,
                )
            )
        ).all()
    await engine.dispose()

    assert first.id == second.id
    assert resource_count == 1
    assert credential_count == 1
    assert sorted(capacities) == [("agent", 1), ("backend", 1)]


async def test_file_sqlite_create_and_delete_share_one_write_guard(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'create-delete.db'}"
    engine = create_async_engine(
        database_url,
        connect_args={"timeout": 0.1},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as seed_session:
        agent, _ = await _seed_backend(
            seed_session, agent_name="create-delete-agent"
        )
        agent_id = agent.id

    create_entered = asyncio.Event()
    release_create = asyncio.Event()
    resource_id: str | None = None

    async def hold_create(_session, resource):
        nonlocal resource_id
        resource_id = resource.id
        create_entered.set()
        await release_create.wait()

    async def create():
        async with factory() as session:
            return await create_db_instance_resource(
                session,
                agent_id=agent_id,
                client_token="create-delete",
                name="orders",
                db_type="polardb_mysql",
                before_commit=hold_create,
            )

    async def delete():
        await create_entered.wait()
        assert resource_id is not None
        async with factory() as session:
            # Model principal resolution on the same session, then explicitly
            # end that read transaction before entering the mutation service.
            await session.execute(select(Agent.id).where(Agent.id == agent_id))
            await session.rollback()
            return await delete_db_instance_resource(
                session, agent_id, resource_id
            )

    create_task = asyncio.create_task(create())
    delete_task = asyncio.create_task(delete())
    await create_entered.wait()
    await asyncio.sleep(0)
    assert not delete_task.done()
    release_create.set()
    created, deleted = await asyncio.gather(create_task, delete_task)

    async with factory() as verify_session:
        stored = await verify_session.get(DBInstanceResource, created.id)
    await engine.dispose()

    assert deleted.id == created.id
    assert stored is not None
    assert stored.status == DBInstanceStatus.DELETING


async def test_concurrent_same_token_returns_winner_when_it_fills_capacity(
    tmp_path,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'full-capacity.db'}"
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as seed_session:
        agent, _ = await _seed_backend(
            seed_session,
            agent_name="full-capacity-agent",
            backend_limit=1,
        )
        agent_id = agent.id

    async def create_once():
        async with factory() as create_session:
            return await create_db_instance_resource(
                create_session,
                agent_id=agent_id,
                client_token="same-token",
                name="orders",
                db_type="polardb_mysql",
            )

    first, second = await asyncio.gather(create_once(), create_once())
    await engine.dispose()

    assert first.id == second.id


async def test_concurrent_same_token_with_different_fingerprints_conflicts_once(
    tmp_path,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'fingerprint-race.db'}"
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as seed_session:
        agent, _ = await _seed_backend(
            seed_session,
            agent_name="fingerprint-race-agent",
        )
        agent_id = agent.id

    async def create_once(name: str):
        async with factory() as create_session:
            return await create_db_instance_resource(
                create_session,
                agent_id=agent_id,
                client_token="same-token",
                name=name,
                db_type="polardb_mysql",
            )

    results = await asyncio.gather(
        create_once("orders"),
        create_once("customers"),
        return_exceptions=True,
    )

    async with factory() as verify_session:
        resource_count = (
            await verify_session.execute(
                select(func.count()).select_from(DBInstanceResource)
            )
        ).scalar_one()
        credential_count = (
            await verify_session.execute(
                select(func.count())
                .select_from(InstanceCredential)
                .where(InstanceCredential.resource_id.is_not(None))
            )
        ).scalar_one()
        capacities = (
            await verify_session.execute(
                select(
                    ProvisioningCapacity.scope_type,
                    ProvisioningCapacity.active_count,
                )
            )
        ).all()
    await engine.dispose()

    assert sum(
        isinstance(result, DBInstanceResource) for result in results
    ) == 1
    assert sum(
        isinstance(result, IdempotencyConflict) for result in results
    ) == 1
    assert resource_count == 1
    assert credential_count == 1
    assert sorted(capacities) == [("agent", 1), ("backend", 1)]
