from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.agent_access import has_agent_access, list_accessible_agent_ids
from server.enterprise_identity.service import (
    upsert_directory_group,
    upsert_directory_membership,
    upsert_external_user,
)
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentUserAssignment,
    AuthProvider,
    Base,
    Department,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    EnterpriseDirectoryMembershipType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    User,
    UserDepartment,
)
from server.models.base import utc_now


@pytest.fixture
async def access_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
        )
        alice = User(
            external_id="alice",
            display_name="Alice",
            auth_provider=AuthProvider.BUILTIN,
        )
        agent = Agent(name="knowledge-agent")
        department = Department(name="Engineering")
        session.add_all([admin, alice, agent, department])
        await session.commit()
        yield session, admin, alice, agent, department
    await engine.dispose()


async def test_department_grant_tracks_current_membership(access_rows) -> None:
    session, admin, alice, agent, department = access_rows
    session.add(
        AgentGroupAssignment.for_department(
            agent_id=agent.id,
            department_id=department.id,
            created_by_user_id=admin.id,
        )
    )
    await session.commit()

    assert not await has_agent_access(session, agent.id, alice.id)
    membership = UserDepartment(
        user_id=alice.id,
        department_id=department.id,
    )
    session.add(membership)
    await session.commit()
    assert await has_agent_access(session, agent.id, alice.id)
    assert await list_accessible_agent_ids(session, alice.id) == {agent.id}

    await session.delete(membership)
    await session.commit()
    assert not await has_agent_access(session, agent.id, alice.id)


async def test_enterprise_group_grant_uses_active_registered_principal(
    access_rows,
) -> None:
    session, admin, alice, agent, _department = access_rows
    session.add(
        AgentGroupAssignment.for_enterprise_group(
            agent_id=agent.id,
            identity_domain="mcp-e2e-domain",
            provider="feishu",
            principal_id="engineering-e2e",
            created_by_user_id=admin.id,
        )
    )
    principal = EnterprisePrincipalAssignment.create(
        pas_user_id=alice.id,
        identity_domain="mcp-e2e-domain",
        provider="feishu",
        principal_type=EnterprisePrincipalType.GROUP,
        principal_id="engineering-e2e",
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
    )
    session.add(principal)
    await session.commit()

    assert await has_agent_access(session, agent.id, alice.id)

    principal.valid_until = utc_now() - timedelta(seconds=1)
    await session.commit()
    assert not await has_agent_access(session, agent.id, alice.id)


async def test_identity_source_group_grant_tracks_synced_membership(
    access_rows,
) -> None:
    session, admin, _alice, agent, _department = access_rows
    source = EnterpriseIdentitySource.create(
        name="Feishu directory",
        provider=IdentitySourceProvider.FEISHU,
        tenant_id="tenant-001",
    )
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC)
    session.add(source)
    await session.flush()
    member = await upsert_external_user(
        session,
        source,
        external_user_id="ou-member",
        display_name="Member",
        email=None,
    )
    group = await upsert_directory_group(
        session,
        source,
        external_group_id="oc-engineering",
        display_name="Engineering",
    )
    membership = await upsert_directory_membership(
        session,
        source,
        external_group_id=group.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="ou-member",
    )
    session.add(
        AgentGroupAssignment.for_identity_source_group(
            agent_id=agent.id,
            identity_source_id=source.id,
            external_group_id=group.external_group_id,
            created_by_user_id=admin.id,
        )
    )
    await session.commit()

    await session.refresh(source)
    assert source.last_synced_at is not None
    assert source.last_synced_at.tzinfo is None

    assert await has_agent_access(session, agent.id, member.id)

    await session.delete(membership)
    await session.commit()
    assert not await has_agent_access(session, agent.id, member.id)

    await upsert_directory_membership(
        session,
        source,
        external_group_id=group.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="ou-member",
    )
    source.last_synced_at = datetime.now(UTC) - timedelta(
        seconds=source.stale_after_seconds + 1
    )
    await session.commit()
    assert not await has_agent_access(session, agent.id, member.id)


async def test_identity_source_all_users_grant_tracks_source_freshness(
    access_rows,
) -> None:
    session, admin, alice, agent, _department = access_rows
    source = EnterpriseIdentitySource.create(
        name="Feishu directory",
        provider=IdentitySourceProvider.FEISHU,
        tenant_id="tenant-001",
    )
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC)
    session.add(source)
    await session.flush()
    member = await upsert_external_user(
        session,
        source,
        external_user_id="ou-member",
        display_name="Member",
        email=None,
    )
    session.add(
        AgentGroupAssignment.for_identity_source_all_users(
            agent_id=agent.id,
            identity_source_id=source.id,
            created_by_user_id=admin.id,
        )
    )
    await session.commit()

    assert await has_agent_access(session, agent.id, member.id)
    assert not await has_agent_access(session, agent.id, alice.id)

    source.last_synced_at = datetime.now(UTC) - timedelta(
        seconds=source.stale_after_seconds + 1
    )
    await session.commit()
    assert not await has_agent_access(session, agent.id, member.id)


async def test_direct_grant_is_independent_from_groups(access_rows) -> None:
    session, admin, alice, agent, _department = access_rows
    session.add(
        AgentUserAssignment(
            agent_id=agent.id,
            user_id=alice.id,
            created_by_user_id=admin.id,
            is_direct=True,
        )
    )
    await session.commit()

    assert await has_agent_access(session, agent.id, alice.id)
