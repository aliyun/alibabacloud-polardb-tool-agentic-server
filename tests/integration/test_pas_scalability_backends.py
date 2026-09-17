from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.identity_sources import list_identity_sources
from server.core.agent_access import has_agent_access
from server.enterprise_identity.service import (
    _StreamingDirectorySink,
    identity_source_snapshot_is_usable,
)
from server.models import (
    Agent,
    AgentGroupAssignment,
    AuthProvider,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
)
from server.polarrag.catalog import claim_space_catalog_sync
from server.polarrag.identity import resolve_acl_context


def _database_url() -> str:
    if os.environ.get("PAS_TEST_SCALABILITY_DATABASE_OK") != "1":
        pytest.skip("PAS_TEST_SCALABILITY_DATABASE_OK=1 is required")
    value = os.environ.get("PAS_TEST_SCALABILITY_DATABASE_URL", "").strip()
    if not value:
        pytest.fail("PAS_TEST_SCALABILITY_DATABASE_URL is required")
    driver = make_url(value).drivername
    if driver not in {
        "sqlite+aiosqlite",
        "mysql+asyncmy",
        "postgresql+asyncpg",
    }:
        pytest.fail("unsupported scalability test database dialect")
    return value


async def test_scalability_database_paths_are_cross_backend() -> None:
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    unique = secrets.token_hex(8)
    now = datetime.now(UTC)
    worker_id = f"identity-{unique}"
    try:
        async with factory() as session:
            admin = User(
                external_id=f"admin-{unique}",
                display_name="Cross-backend administrator",
                auth_provider=AuthProvider.BUILTIN,
            )
            agent = Agent(name=f"cross-backend-agent-{unique}")
            source = EnterpriseIdentitySource.create(
                name=f"Cross-backend source {unique}",
                provider=IdentitySourceProvider.FEISHU,
                tenant_id=f"tenant-{unique}",
            )
            source.status = EnterpriseIdentitySourceStatus.ACTIVE
            source.last_synced_at = now
            source.sync_worker_id = worker_id
            session.add_all([admin, agent, source])
            await session.flush()
            instance = PolarRAGInstance(
                name=f"cross-backend-polarrag-{unique}",
                scheme="https",
                host="polarrag.example.test",
                port=443,
                username_ciphertext="encrypted",
                password_ciphertext="encrypted",
                status=PolarRAGInstanceStatus.ACTIVE,
                created_by=admin.id,
            )
            session.add(instance)
            await session.flush()
            space = PolarRAGSpace(
                polarrag_instance_id=instance.id,
                space_id=f"space-{unique}",
                name="Cross-backend Space",
                identity_domain=source.tenant_id,
                enabled=True,
            )
            session.add(space)
            await session.flush()
            session.add(
                EnterpriseIdentitySourceSpaceBinding(
                    identity_source_id=source.id,
                    knowledge_space_id=space.knowledge_space_id,
                )
            )
            await session.commit()

            sink = _StreamingDirectorySink(
                session,
                source,
                sync_worker_id=worker_id,
            )
            await sink.begin_sync(now.isoformat())
            await sink.upsert_users([{"id": f"user-{unique}", "display_name": "Directory user"}])
            await sink.upsert_groups(
                [
                    {
                        "id": f"parent-{unique}",
                        "display_name": "Parent department",
                        "principal_type": EnterpriseDirectoryPrincipalType.DEPARTMENT.value,
                    },
                    {
                        "id": f"child-{unique}",
                        "display_name": "Child department",
                        "principal_type": EnterpriseDirectoryPrincipalType.DEPARTMENT.value,
                    },
                ]
            )
            await sink.upsert_memberships(
                [
                    {
                        "group_id": f"parent-{unique}",
                        "member_type": "group",
                        "member_id": f"child-{unique}",
                    },
                    {
                        "group_id": f"child-{unique}",
                        "member_type": "user",
                        "member_id": f"user-{unique}",
                    },
                ]
            )
            await sink.complete_sync()

            user_id = await session.scalar(
                select(UserExternalIdentity.user_id).where(
                    UserExternalIdentity.identity_provider == f"feishu:{source.tenant_id}",
                    UserExternalIdentity.external_subject == f"user-{unique}",
                )
            )
            assert user_id is not None
            session.add(
                AgentGroupAssignment.for_identity_source_group(
                    agent_id=agent.id,
                    identity_source_id=source.id,
                    external_group_id=f"parent-{unique}",
                    created_by_user_id=admin.id,
                )
            )
            await session.commit()

            page = await list_identity_sources(
                offset=0,
                limit=1,
                search=unique,
                _admin=admin,
                session=session,
            )
            assert page["total"] == 1
            assert page["items"][0]["id"] == source.id
            assert await has_agent_access(session, agent.id, user_id)

            acl_context = await resolve_acl_context(
                session,
                user_id,
                space.knowledge_space_id,
                now=now,
            )
            assert {
                (principal["type"], principal["id"])
                for principal in acl_context["principals"]
                if principal["provider"] == "feishu"
            } == {
                ("user", f"user-{unique}"),
                ("department", f"child-{unique}"),
                ("department", f"parent-{unique}"),
            }

            source.status = EnterpriseIdentitySourceStatus.STALE
            source.last_synced_at = now - timedelta(seconds=1)
            await session.commit()
            assert identity_source_snapshot_is_usable(source, now=now)
            assert await has_agent_access(session, agent.id, user_id)

            assert await claim_space_catalog_sync(
                session,
                space.knowledge_space_id,
                f"catalog-{unique}",
            )
            assert not await claim_space_catalog_sync(
                session,
                space.knowledge_space_id,
                f"catalog-duplicate-{unique}",
            )
    finally:
        await engine.dispose()
