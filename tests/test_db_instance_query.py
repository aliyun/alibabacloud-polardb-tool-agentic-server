from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.auth.principal import Principal, PrincipalKind
from server.config import reset_config
from server.core.crypto import encrypt
from server.core.db_instance_query import (
    InvalidDBInstanceFilter,
    query_db_instances,
)
from server.core.db_instance_service import (
    UnsupportedDBType,
    describe_db_instance_resource,
)
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AllocationMode,
    AuthProvider,
    Base,
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceStatus,
    Permission,
    ProvisioningBackend,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


@pytest.fixture(autouse=True)
def encryption_config(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_config()
    yield
    reset_config()


async def _physical_binding(
    session: AsyncSession,
    *,
    agent: Agent,
    admin: User,
    name: str = "Production",
    enabled: bool = True,
    credential_status: CredentialStatus = CredentialStatus.ACTIVE,
) -> Instance:
    instance = Instance(
        cluster_id=f"pc-{name.lower()}",
        name=name,
        usage="Finance reporting",
        allocation_mode=AllocationMode.REGISTERED,
        status=InstanceStatus.ACTIVE,
    )
    session.add(instance)
    await session.flush()
    credential = InstanceCredential(
        instance_id=instance.id,
        name=f"{name}-agent",
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=CredentialCapability.READWRITE,
        status=credential_status,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
    )
    session.add(credential)
    await session.flush()
    binding = AgentInstanceBinding(
        agent_id=agent.id,
        instance_id=instance.id,
        credential_id=credential.id,
        permission=Permission.READWRITE,
        enabled=enabled,
        created_by_user_id=admin.id,
    )
    binding.capabilities = [
        AgentInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_DESCRIBE
        )
    ]
    session.add(binding)
    await session.flush()
    return instance


async def _resource(
    session: AsyncSession,
    *,
    agent: Agent,
    admin: User,
    created_at: datetime,
    token: str,
) -> DBInstanceResource:
    backend_instance = Instance(
        cluster_id=f"pc-backend-{token}",
        name=f"Backend {token}",
    )
    session.add(backend_instance)
    await session.flush()
    credential = InstanceCredential(
        instance_id=backend_instance.id,
        name=f"admin-{token}",
        purpose=CredentialPurpose.PROVISIONING_ADMIN,
        capability=CredentialCapability.ADMIN,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
    )
    session.add(credential)
    await session.flush()
    backend = ProvisioningBackend(
        instance_id=backend_instance.id,
        admin_credential_id=credential.id,
        max_active_resources=10,
    )
    session.add(backend)
    await session.flush()
    resource = DBInstanceResource(
        owner_agent_id=agent.id,
        backend_id=backend.id,
        client_token=token,
        request_fingerprint=token.ljust(64, "0")[:64],
        name=f"Resource {token}",
        status=DBInstanceStatus.READY,
        database_name=f"db_{token.replace('-', '_')}",
        created_at=created_at,
    )
    session.add(resource)
    await session.flush()
    session.add(
        InstanceCredential(
            resource_id=resource.id,
            name=f"resource-{token}",
            purpose=CredentialPurpose.RESOURCE_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("resource-user"),
            password_ciphertext=encrypt("resource-password"),
            database_name=resource.database_name,
        )
    )
    await session.flush()
    return resource


async def test_list_unifies_bound_and_provisioned_sources(session):
    admin = User(external_id="admin-query", display_name="Admin")
    agent = Agent(name="query-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    physical = await _physical_binding(
        session, agent=agent, admin=admin
    )
    resource = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=physical.created_at + timedelta(seconds=1),
        token="union",
    )
    await session.commit()

    page = await query_db_instances(
        session, Principal(PrincipalKind.AGENT, agent.id), limit=50
    )

    assert [
        (item.source, item.db_instance_id) for item in page.instances
    ] == [
        ("provisioned", resource.id),
        ("bound", physical.id),
    ]
    assert page.instances[0].capabilities == (
        "list",
        "describe",
        "credentials_read",
        "delete",
        "run_sql_read",
        "run_sql_write",
    )
    assert page.instances[0].usage is None
    assert page.instances[1].usage == "Finance reporting"
    assert page.has_more is False
    assert page.next_cursor is None


async def test_system_binding_persisted_management_capability_is_ignored(
    session,
):
    user = User(
        external_id="system-list-user",
        display_name="System User",
        auth_provider=AuthProvider.BUILTIN,
    )
    instance = Instance(
        cluster_id="pc-system-list",
        name="System instance",
        allocation_mode=AllocationMode.AUTO_PROVISIONED,
        owner=user,
    )
    binding = UserInstanceBinding(
        user=user,
        instance=instance,
        permission=Permission.READWRITE,
        origin=BindingOrigin.SYSTEM,
    )
    session.add_all([user, instance, binding])
    await session.commit()

    hidden = await query_db_instances(
        session, Principal(PrincipalKind.USER, user.id), limit=50
    )
    assert hidden.instances == []

    session.add_all([
        UserInstanceBindingCapability(
            binding_id=binding.id,
            capability=BindingCapability.DB_INSTANCE_LIST
        )
    ])
    await session.commit()
    still_hidden = await query_db_instances(
        session, Principal(PrincipalKind.USER, user.id), limit=50
    )
    assert still_hidden.instances == []


async def test_admin_authorization_preserves_auto_provisioned_source(session):
    user = User(
        external_id="admin-authorized-auto-user",
        display_name="Authorized User",
        auth_provider=AuthProvider.BUILTIN,
    )
    instance = Instance(
        cluster_id="pc-admin-authorized-auto",
        name="Auto instance",
        allocation_mode=AllocationMode.AUTO_PROVISIONED,
        owner=user,
    )
    credential = InstanceCredential(
        instance=instance,
        name="auto-direct",
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=CredentialCapability.READWRITE,
        username_ciphertext=encrypt("auto-user"),
        password_ciphertext=encrypt("auto-password"),
    )
    binding = UserInstanceBinding(
        user=user,
        instance=instance,
        credential=credential,
        permission=Permission.READWRITE,
        origin=BindingOrigin.ADMIN,
    )
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_LIST
        )
    ]
    session.add_all([user, instance, credential, binding])
    await session.commit()

    visible = await query_db_instances(
        session, Principal(PrincipalKind.USER, user.id), limit=50
    )
    assert [(item.source, item.permission) for item in visible.instances] == [
        ("auto_provisioned", None)
    ]


async def test_list_excludes_disabled_binding_and_revoked_credential(session):
    admin = User(external_id="admin-hidden", display_name="Admin")
    agent = Agent(name="hidden-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    await _physical_binding(
        session, agent=agent, admin=admin, name="Disabled", enabled=False
    )
    await _physical_binding(
        session,
        agent=agent,
        admin=admin,
        name="Revoked",
        credential_status=CredentialStatus.REVOKED,
    )
    await session.commit()

    page = await query_db_instances(
        session, Principal(PrincipalKind.AGENT, agent.id), limit=50
    )
    assert page.instances == []


async def test_keyset_pagination_is_stable_across_insertions_and_ties(session):
    admin = User(external_id="admin-pages", display_name="Admin")
    agent = Agent(name="page-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    timestamp = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    resources = [
        await _resource(
            session,
            agent=agent,
            admin=admin,
            created_at=timestamp,
            token=f"page-{index}",
        )
        for index in range(3)
    ]
    await session.commit()
    expected = sorted((item.id for item in resources), reverse=True)

    first = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        limit=2,
        source="provisioned",
    )
    assert [item.db_instance_id for item in first.instances] == expected[:2]
    assert first.has_more is True
    assert first.next_cursor

    await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=timestamp + timedelta(seconds=1),
        token="inserted-between-pages",
    )
    await session.commit()
    second = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        cursor=first.next_cursor,
        limit=2,
        source="provisioned",
    )
    assert [item.db_instance_id for item in second.instances] == expected[2:]
    assert second.has_more is False


async def test_list_filters_do_not_reveal_other_sources(session):
    admin = User(external_id="admin-filter", display_name="Admin")
    agent = Agent(name="filter-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    await _physical_binding(session, agent=agent, admin=admin)
    resource = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=datetime.now(timezone.utc),
        token="filtered",
    )
    await session.commit()

    page = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        db_type="polardb_mysql",
        source="provisioned",
        status="READY",
        limit=50,
    )
    assert [item.db_instance_id for item in page.instances] == [resource.id]


async def test_default_list_excludes_deleted_before_page_boundaries(session):
    admin = User(external_id="admin-deleted-pages", display_name="Admin")
    agent = Agent(name="deleted-page-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    start = datetime(2026, 7, 25, 14, 0, tzinfo=timezone.utc)
    newest_deleted = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=start + timedelta(seconds=5),
        token="deleted-newest",
    )
    first_live = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=start + timedelta(seconds=4),
        token="live-first",
    )
    middle_deleted = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=start + timedelta(seconds=3),
        token="deleted-middle",
    )
    second_live = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=start + timedelta(seconds=2),
        token="live-second",
    )
    boundary_deleted = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=start + timedelta(seconds=1),
        token="deleted-boundary",
    )
    third_live = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=start,
        token="live-third",
    )
    newest_deleted.status = DBInstanceStatus.DELETED
    middle_deleted.status = DBInstanceStatus.DELETED
    boundary_deleted.status = DBInstanceStatus.DELETED
    await session.commit()

    first = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        limit=2,
        source="provisioned",
    )
    assert [item.db_instance_id for item in first.instances] == [
        first_live.id,
        second_live.id,
    ]
    assert first.has_more is True
    assert first.next_cursor is not None

    second = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        cursor=first.next_cursor,
        limit=2,
        source="PROVISIONED",
    )
    assert [item.db_instance_id for item in second.instances] == [
        third_live.id
    ]
    assert second.has_more is False
    assert second.next_cursor is None


async def test_explicit_deleted_history_has_only_usable_capabilities(session):
    admin = User(external_id="admin-deleted-history", display_name="Admin")
    agent = Agent(name="deleted-history-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    deleted = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=datetime.now(timezone.utc),
        token="deleted-history",
    )
    deleted.status = DBInstanceStatus.DELETED
    await session.commit()

    page = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        status="deleted",
    )
    assert [item.db_instance_id for item in page.instances] == [deleted.id]
    assert page.instances[0].status == "DELETED"
    assert page.instances[0].capabilities == ("list", "describe")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            DBInstanceStatus.CREATING,
            ("list", "describe", "delete"),
        ),
        (
            DBInstanceStatus.READY,
            (
                "list",
                "describe",
                "credentials_read",
                "delete",
                "run_sql_read",
                "run_sql_write",
            ),
        ),
        (
            DBInstanceStatus.FAILED,
            ("list", "describe", "delete"),
        ),
        (
            DBInstanceStatus.DELETING,
            ("list", "describe", "delete"),
        ),
        (
            DBInstanceStatus.DELETE_FAILED,
            ("list", "describe", "delete"),
        ),
        (
            DBInstanceStatus.DELETED,
            ("list", "describe"),
        ),
    ],
)
async def test_list_and_describe_share_resource_capabilities(
    session, status, expected
):
    admin = User(
        external_id=f"admin-capabilities-{status.value}",
        display_name="Admin",
    )
    agent = Agent(name=f"capabilities-agent-{status.value}")
    session.add_all([admin, agent])
    await session.flush()
    resource = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=datetime.now(timezone.utc),
        token=f"capabilities-{status.value}",
    )
    resource.status = status
    await session.commit()

    page = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        status=status.value,
    )
    described = await describe_db_instance_resource(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        resource.id,
    )

    assert page.instances[0].capabilities == expected
    assert tuple(described["capabilities"]) == expected
    if status == DBInstanceStatus.DELETED:
        assert not {
            "host",
            "port",
            "database",
            "username",
            "password",
        } & set(described)


async def test_ready_resource_does_not_claim_revoked_credentials(session):
    admin = User(external_id="admin-revoked-resource", display_name="Admin")
    agent = Agent(name="revoked-resource-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    resource = await _resource(
        session,
        agent=agent,
        admin=admin,
        created_at=datetime.now(timezone.utc),
        token="revoked-resource",
    )
    await session.refresh(resource, ["credentials"])
    resource.credentials[0].status = CredentialStatus.REVOKED
    await session.commit()

    page = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        status="READY",
    )
    assert page.instances[0].capabilities == (
        "list",
        "describe",
        "delete",
    )


@pytest.mark.parametrize(
    ("field", "value", "exception", "code"),
    [
        ("db_type", "postgres", UnsupportedDBType, "UNSUPPORTED_DB_TYPE"),
        ("source", "pool", InvalidDBInstanceFilter, "INVALID_ARGUMENT"),
        ("status", "gone", InvalidDBInstanceFilter, "INVALID_ARGUMENT"),
        ("db_type", 1, UnsupportedDBType, "UNSUPPORTED_DB_TYPE"),
        ("source", 1, InvalidDBInstanceFilter, "INVALID_ARGUMENT"),
        ("status", 1, InvalidDBInstanceFilter, "INVALID_ARGUMENT"),
    ],
)
async def test_invalid_filters_fail_before_cursor_decode(
    session, field, value, exception, code
):
    kwargs = {field: value}
    with pytest.raises(exception) as error:
        await query_db_instances(
            session,
            Principal(PrincipalKind.AGENT, "missing-agent"),
            cursor="also-invalid",
            **kwargs,  # type: ignore[arg-type]
        )
    assert error.value.code == code


async def test_cursor_accepts_canonical_equivalent_filter_case(session):
    admin = User(external_id="admin-canonical-filter", display_name="Admin")
    agent = Agent(name="canonical-filter-agent", creator=admin)
    session.add_all([admin, agent])
    await session.flush()
    timestamp = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    for index in range(2):
        await _resource(
            session,
            agent=agent,
            admin=admin,
            created_at=timestamp - timedelta(seconds=index),
            token=f"canonical-{index}",
        )
    await session.commit()

    first = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        limit=1,
        db_type="POLARDB_MYSQL",
        source="PROVISIONED",
        status="ready",
    )
    second = await query_db_instances(
        session,
        Principal(PrincipalKind.AGENT, agent.id),
        cursor=first.next_cursor,
        limit=1,
        db_type="polardb_mysql",
        source="provisioned",
        status="READY",
    )
    assert len(first.instances) == len(second.instances) == 1
    assert (
        first.instances[0].db_instance_id
        != second.instances[0].db_instance_id
    )
