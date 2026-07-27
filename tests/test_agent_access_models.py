from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

import server.models as models


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session

    await engine.dispose()


def test_target_model_interfaces_are_exported():
    expected = {
        "Agent",
        "AgentStatus",
        "InstanceEngine",
        "InstanceTopology",
        "AllocationMode",
        "InstanceCredential",
        "CredentialPurpose",
        "CredentialCapability",
        "BindingCapability",
        "AgentInstanceBinding",
        "AgentProvisioningBinding",
        "ProvisioningBackend",
        "ProvisioningBackendHealth",
        "ProvisioningCapacity",
        "DBInstanceResource",
    }

    assert expected <= set(models.__all__)


async def test_credential_owner_xor_is_enforced(session: AsyncSession):
    session.add(
        models.InstanceCredential(
            name="invalid",
            purpose=models.CredentialPurpose.DIRECT_ACCESS,
            capability=models.CredentialCapability.READONLY,
            username_ciphertext="u",
            password_ciphertext="p",
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()


async def test_agent_token_is_one_to_one(session: AsyncSession):
    agent = models.Agent(name="schema-test-agent")
    session.add(agent)
    await session.commit()
    session.add_all(
        [
            models.AgentAPIToken(agent_id=agent.id, token_prefix="a", token_hash="a" * 64),
            models.AgentAPIToken(agent_id=agent.id, token_prefix="b", token_hash="b" * 64),
        ]
    )

    with pytest.raises(IntegrityError):
        await session.commit()


def test_instance_dimensions_and_status_are_separate():
    assert {item.value for item in models.InstanceEngine} == {"polardb_mysql"}
    assert {item.value for item in models.InstanceTopology} == {
        "single_tenant",
        "multitenant",
    }
    assert {item.value for item in models.AllocationMode} == {
        "auto_provisioned",
        "pooled",
        "registered",
    }
    assert {item.value for item in models.InstanceStatus} == {
        "creating",
        "active",
        "stopped",
        "failed",
    }


def test_binding_capabilities_use_stable_namespaced_values():
    assert {item.value for item in models.BindingCapability} == {
        "db_instance:list",
        "db_instance:describe",
        "db_instance:credentials:read",
        "sql:read",
        "sql:write",
    }


@pytest.mark.parametrize(
    "association_type",
    [
        models.UserInstanceBindingCapability,
        models.AgentInstanceBindingCapability,
    ],
)
@pytest.mark.parametrize(
    "capability",
    ["unknown:capability", "db_instance:create", "db_instance:delete"],
)
async def test_binding_capability_orm_rejects_non_persistable_strings(
    session: AsyncSession,
    association_type,
    capability: str,
):
    session.add(association_type(binding_id="missing-binding", capability=capability))

    with pytest.raises(StatementError, match="not among the defined enum values"):
        await session.flush()


@pytest.mark.parametrize(
    "dialect",
    [mysql.dialect(), postgresql.dialect()],
    ids=["mysql", "postgresql"],
)
def test_target_enum_ddl_is_portable_and_constrained(dialect):
    assert models.Instance.__table__.c.status.type.compile(dialect=dialect) == "VARCHAR(32)"
    assert models.AgentInstanceBinding.__table__.c.permission.type.compile(dialect=dialect) == "VARCHAR(32)"

    for table, constraint_name in [
        (
            models.UserInstanceBindingCapability.__table__,
            "ck_user_instance_binding_capabilities_value",
        ),
        (
            models.AgentInstanceBindingCapability.__table__,
            "ck_agent_instance_binding_capabilities_value",
        ),
    ]:
        assert table.c.capability.type.compile(dialect=dialect) == "VARCHAR(64)"
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert constraint_name in ddl
        assert "db_instance:create" not in ddl
        assert "db_instance:delete" not in ddl
