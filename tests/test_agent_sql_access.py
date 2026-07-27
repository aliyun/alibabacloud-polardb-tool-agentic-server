from __future__ import annotations

import base64
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.crypto import encrypt
from server.mcp.tools import reset_gateway, set_gateway
from server.mcp.tools.agent_sql_access import (
    AgentSQLAccess,
    resolve_agent_sql_access,
)
from server.mcp.tools.handlers import (
    handle_run_sql,
    handle_run_sql_transaction,
)
from server.mcp.tools.schema_handler import handle_describe_schema
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AllocationMode,
    AuditLog,
    Base,
    BindingCapability,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendStatus,
    User,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(b"a" * 32).decode(),
    )


class RecordingGateway:
    def __init__(self):
        self.execute_kwargs = None
        self.transaction_kwargs = None
        self.parameterized_calls = []

    async def execute(self, **kwargs):
        self.execute_kwargs = kwargs
        return {
            "columns": ["value"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }

    async def execute_transaction(self, **kwargs):
        self.transaction_kwargs = kwargs
        return [
            {
                "columns": ["value"],
                "rows": [[1]],
                "row_count": 1,
                "truncated": False,
            }
            for _ in kwargs["sql_statements"]
        ]

    async def execute_parameterized(self, **kwargs):
        self.parameterized_calls.append(kwargs)
        return {
            "columns": [
                "TABLE_NAME",
                "TABLE_COMMENT",
                "TABLE_ROWS",
                "CREATE_TIME",
            ],
            "rows": [["orders", "Orders", 3, None]],
        }


@pytest.fixture
def gateway():
    value = RecordingGateway()
    set_gateway(value)
    yield value
    reset_gateway()


async def _bound_agent(
    session,
    *,
    permission: Permission = Permission.READONLY,
    credential_capability: CredentialCapability = (CredentialCapability.READONLY),
    enabled: bool = True,
    credential_status: CredentialStatus = CredentialStatus.ACTIVE,
):
    creator = User(
        external_id="agent-sql-creator",
        display_name="Agent SQL Creator",
    )
    agent = Agent(name="agent-sql", creator=creator)
    instance = Instance(
        cluster_id="pc-agent-sql",
        name="Agent SQL Instance",
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.REGISTERED,
        status=InstanceStatus.ACTIVE,
        host="database.example.invalid",
        port=3306,
    )
    session.add_all([creator, agent, instance])
    await session.flush()
    credential = InstanceCredential(
        instance_id=instance.id,
        name="agent-sql-direct",
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=credential_capability,
        status=credential_status,
        username_ciphertext=encrypt("agent-user"),
        password_ciphertext=encrypt("agent-password"),
        database_name="application",
        created_by_user_id=creator.id,
    )
    session.add(credential)
    await session.flush()
    binding = AgentInstanceBinding(
        agent_id=agent.id,
        instance_id=instance.id,
        credential_id=credential.id,
        permission=permission,
        enabled=enabled,
        created_by_user_id=creator.id,
    )
    capabilities = [BindingCapability.SQL_READ]
    if permission == Permission.READWRITE:
        capabilities.append(BindingCapability.SQL_WRITE)
    binding.capabilities = [AgentInstanceBindingCapability(capability=capability) for capability in capabilities]
    session.add(binding)
    await session.commit()
    return agent, instance, credential, binding


async def _provisioned_agent(
    session,
    *,
    status: DBInstanceStatus = DBInstanceStatus.READY,
    owner: Agent | None = None,
):
    creator = User(
        external_id=f"resource-creator-{status.value}",
        display_name="Resource Creator",
    )
    agent = owner or Agent(
        name=f"resource-agent-{status.value}",
        creator=creator,
    )
    instance = Instance(
        cluster_id=f"pc-resource-{status.value}",
        name="Resource Backend",
        topology=InstanceTopology.MULTITENANT,
        allocation_mode=AllocationMode.REGISTERED,
        status=InstanceStatus.ACTIVE,
        host="resource.example.invalid",
        port=3306,
    )
    session.add_all([creator, agent, instance])
    await session.flush()
    admin_credential = InstanceCredential(
        instance_id=instance.id,
        name="provisioning-admin",
        purpose=CredentialPurpose.PROVISIONING_ADMIN,
        capability=CredentialCapability.ADMIN,
        status=CredentialStatus.ACTIVE,
        username_ciphertext=encrypt("admin"),
        password_ciphertext=encrypt("admin-password"),
        created_by_user_id=creator.id,
    )
    session.add(admin_credential)
    await session.flush()
    backend = ProvisioningBackend(
        instance_id=instance.id,
        admin_credential_id=admin_credential.id,
        status=ProvisioningBackendStatus.ACTIVE,
        max_active_resources=10,
    )
    session.add(backend)
    await session.flush()
    resource = DBInstanceResource(
        owner_agent_id=agent.id,
        backend_id=backend.id,
        client_token=f"token-{status.value}",
        request_fingerprint=f"fingerprint-{status.value}",
        status=status,
        database_name="agent_database",
    )
    session.add(resource)
    await session.flush()
    credential = InstanceCredential(
        resource_id=resource.id,
        name="resource-access",
        purpose=CredentialPurpose.RESOURCE_ACCESS,
        capability=CredentialCapability.READWRITE,
        status=CredentialStatus.ACTIVE,
        version=1,
        username_ciphertext=encrypt("resource-user"),
        password_ciphertext=encrypt("resource-password"),
        database_name=resource.database_name,
        created_by_user_id=creator.id,
    )
    session.add(credential)
    await session.commit()
    return agent, instance, resource, credential


def _error_code(result: dict) -> str:
    assert result["isError"] is True
    return json.loads(result["content"][0]["text"])["error"]


async def test_resolves_agent_direct_binding_to_sql_access(session):
    agent, instance, credential, binding = await _bound_agent(session)

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=instance.id,
        database=None,
    )

    assert isinstance(result, AgentSQLAccess)
    assert result.agent is agent
    assert result.instance.id == instance.id
    assert result.binding.id == binding.id
    assert result.credential.id == credential.id
    assert result.permission == Permission.READONLY
    assert result.database == "application"


async def test_explicit_database_overrides_credential_default(session):
    agent, instance, _, _ = await _bound_agent(session)

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=instance.id,
        database="reporting",
    )

    assert isinstance(result, AgentSQLAccess)
    assert result.database == "reporting"


async def test_resolves_owned_ready_resource(session):
    agent, instance, resource, credential = await _provisioned_agent(session)

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=resource.id,
        database=None,
    )

    assert isinstance(result, AgentSQLAccess)
    assert result.public_instance_id == resource.id
    assert result.source == "provisioned"
    assert result.instance.id == instance.id
    assert result.resource is not None
    assert result.resource.id == resource.id
    assert result.binding is None
    assert result.credential.id == credential.id
    assert result.permission == Permission.READWRITE
    assert result.database == "agent_database"


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (DBInstanceStatus.CREATING, "INSTANCE_STARTING"),
        (DBInstanceStatus.FAILED, "INSTANCE_FAILED"),
        (DBInstanceStatus.DELETING, "INSTANCE_DELETING"),
        (DBInstanceStatus.DELETE_FAILED, "INSTANCE_DELETE_FAILED"),
    ],
)
async def test_rejects_unavailable_provisioned_resource(
    session,
    status,
    error_code,
):
    agent, _, resource, _ = await _provisioned_agent(
        session,
        status=status,
    )

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=resource.id,
        database=None,
    )

    assert _error_code(result) == error_code


async def test_rejects_foreign_resource(session):
    owner, _, resource, _ = await _provisioned_agent(session)
    other = Agent(name="foreign-resource-agent")
    session.add(other)
    await session.commit()
    assert other.id != owner.id

    result = await resolve_agent_sql_access(
        other,
        session,
        instance_id=resource.id,
        database=None,
    )

    assert _error_code(result) == "INSTANCE_NOT_ACCESSIBLE"


async def test_rejects_provisioned_database_override(session):
    agent, _, resource, _ = await _provisioned_agent(session)

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=resource.id,
        database="another_database",
    )

    assert _error_code(result) == "INVALID_ARGUMENT"
    payload = json.loads(result["content"][0]["text"])
    assert "database must be omitted" in payload["message"]
    assert resource.database_name in payload["message"]


async def test_requires_explicit_instance_id_for_agent(session):
    agent, _, _, _ = await _bound_agent(session)

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=None,
        database=None,
    )

    assert _error_code(result) == "INVALID_ARGUMENT"


async def test_rejects_instance_bound_to_another_agent(session):
    _, instance, _, _ = await _bound_agent(session)
    other = Agent(name="other-agent-sql")
    session.add(other)
    await session.commit()

    result = await resolve_agent_sql_access(
        other,
        session,
        instance_id=instance.id,
        database=None,
    )

    assert _error_code(result) == "INSTANCE_NOT_ACCESSIBLE"


async def test_rejects_disabled_binding(session):
    agent, instance, _, _ = await _bound_agent(
        session,
        enabled=False,
    )

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=instance.id,
        database=None,
    )

    assert _error_code(result) == "INSTANCE_NOT_ACCESSIBLE"


async def test_rejects_revoked_direct_credential(session):
    agent, instance, _, _ = await _bound_agent(
        session,
        credential_status=CredentialStatus.REVOKED,
    )

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=instance.id,
        database=None,
    )

    assert _error_code(result) == "INSTANCE_NOT_ACCESSIBLE"


async def test_effective_permission_respects_readonly_credential(session):
    agent, instance, _, _ = await _bound_agent(
        session,
        permission=Permission.READWRITE,
        credential_capability=CredentialCapability.READONLY,
    )

    result = await resolve_agent_sql_access(
        agent,
        session,
        instance_id=instance.id,
        database=None,
    )

    assert isinstance(result, AgentSQLAccess)
    assert result.permission == Permission.READONLY


async def test_agent_run_sql_uses_binding_credential_and_namespaced_cache_key(
    session,
    gateway,
):
    agent, instance, _, _ = await _bound_agent(session)

    result = await handle_run_sql(
        agent,
        session,
        sql="SELECT 1",
        instance_id=instance.id,
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["permission"] == "readonly"
    assert payload["instance_id"] == instance.id
    assert gateway.execute_kwargs["user"] == "agent-user"
    assert gateway.execute_kwargs["password"] == "agent-password"
    assert gateway.execute_kwargs["database"] == "application"
    assert gateway.execute_kwargs["user_id"] == f"agent:{agent.id}"
    assert gateway.execute_kwargs["instance_id"] == instance.id


async def test_agent_run_sql_uses_provisioned_resource_identity(
    session,
    gateway,
):
    agent, instance, resource, _ = await _provisioned_agent(session)

    result = await handle_run_sql(
        agent,
        session,
        sql="SELECT 1",
        instance_id=resource.id,
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["instance_id"] == resource.id
    assert "cluster_id" not in payload
    assert payload["permission"] == "readwrite"
    assert gateway.execute_kwargs["host"] == instance.host
    assert gateway.execute_kwargs["user"] == "resource-user"
    assert gateway.execute_kwargs["password"] == "resource-password"
    assert gateway.execute_kwargs["database"] == resource.database_name
    assert gateway.execute_kwargs["instance_id"] == resource.id
    audit = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.action == "run_sql")
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().first()
    assert audit is not None
    assert audit.instance_id == instance.id
    assert audit.target_type == "db_instance_resource"
    assert audit.target_id == resource.id


async def test_readonly_agent_run_sql_rejects_write(session, gateway):
    agent, instance, _, _ = await _bound_agent(session)

    result = await handle_run_sql(
        agent,
        session,
        sql="INSERT INTO t VALUES (1)",
        instance_id=instance.id,
    )

    assert _error_code(result) == "READ_ONLY_ACCESS"
    assert gateway.execute_kwargs is None


async def test_agent_run_sql_rejects_branch(session, gateway):
    agent, instance, _, _ = await _bound_agent(session)

    result = await handle_run_sql(
        agent,
        session,
        sql="SELECT 1",
        instance_id=instance.id,
        branch="feature",
    )

    assert _error_code(result) == "INVALID_ARGUMENT"
    assert gateway.execute_kwargs is None


async def test_readonly_agent_transaction_executes_read_statements(
    session,
    gateway,
):
    agent, instance, _, _ = await _bound_agent(session)

    result = await handle_run_sql_transaction(
        agent,
        session,
        sql_statements=["SELECT 1", "SHOW TABLES"],
        instance_id=instance.id,
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["permission"] == "readonly"
    assert payload["statement_count"] == 2
    assert gateway.transaction_kwargs["user_id"] == f"agent:{agent.id}"
    assert gateway.transaction_kwargs["database"] == "application"


async def test_agent_transaction_uses_provisioned_resource_identity(
    session,
    gateway,
):
    agent, _, resource, _ = await _provisioned_agent(session)

    result = await handle_run_sql_transaction(
        agent,
        session,
        sql_statements=["INSERT INTO t VALUES (1)"],
        instance_id=resource.id,
    )

    payload = json.loads(result["content"][0]["text"])
    assert payload["instance_id"] == resource.id
    assert "cluster_id" not in payload
    assert payload["permission"] == "readwrite"
    assert gateway.transaction_kwargs["user"] == "resource-user"
    assert gateway.transaction_kwargs["database"] == resource.database_name
    assert gateway.transaction_kwargs["instance_id"] == resource.id


async def test_readonly_agent_transaction_rejects_write(
    session,
    gateway,
):
    agent, instance, _, _ = await _bound_agent(session)

    result = await handle_run_sql_transaction(
        agent,
        session,
        sql_statements=["SELECT 1", "UPDATE t SET value = 2"],
        instance_id=instance.id,
    )

    assert _error_code(result) == "READ_ONLY_ACCESS"
    assert gateway.transaction_kwargs is None


async def test_agent_describe_schema_uses_direct_binding_credential(
    session,
    gateway,
):
    agent, instance, _, _ = await _bound_agent(session)

    result = await handle_describe_schema(
        agent,
        session,
        instance_id=instance.id,
        include_columns=False,
    )

    payload = json.loads(result["content"][0]["text"])
    assert [table["table_name"] for table in payload["tables"]] == ["orders"]
    assert len(gateway.parameterized_calls) == 1
    request = gateway.parameterized_calls[0]
    assert request["user"] == "agent-user"
    assert request["password"] == "agent-password"
    assert request["database"] == "application"
    assert request["user_id"] == f"agent:{agent.id}"
    assert request["instance_id"] == instance.id


async def test_agent_describe_schema_uses_provisioned_resource_identity(
    session,
    gateway,
):
    agent, _, resource, _ = await _provisioned_agent(session)

    result = await handle_describe_schema(
        agent,
        session,
        instance_id=resource.id,
        include_columns=False,
    )

    payload = json.loads(result["content"][0]["text"])
    assert [table["table_name"] for table in payload["tables"]] == [
        "orders"
    ]
    request = gateway.parameterized_calls[0]
    assert request["user"] == "resource-user"
    assert request["password"] == "resource-password"
    assert request["database"] == resource.database_name
    assert request["instance_id"] == resource.id
