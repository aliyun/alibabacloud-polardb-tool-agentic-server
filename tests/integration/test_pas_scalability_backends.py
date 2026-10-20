from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.api.identity_sources import list_identity_sources
from server.core.agent_access import has_agent_access
from server.enterprise_identity.service import (
    _StreamingDirectorySink,
    apply_feishu_directory_event,
    identity_source_snapshot_is_usable,
    replace_directory_snapshot,
    upsert_directory_group,
    upsert_external_user,
)
from server.models import (
    Agent,
    AgentGroupAssignment,
    AuthProvider,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    ExternalTokenSession,
    OAuthRefreshToken,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
    UserRefreshToken,
    UserStatus,
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


@pytest.mark.parametrize("sync_mode", ["streaming", "snapshot", "event"])
async def test_directory_removal_preserves_account_ownership_and_revokes_sessions(sync_mode) -> None:
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    unique = secrets.token_hex(8)
    now = datetime.now(UTC).replace(microsecond=0)
    old_revocation = now - timedelta(days=1)
    try:
        async with factory() as session:
            if engine.dialect.name == "mysql":
                # The regression must also pass with derived-table merging enabled.
                await session.execute(text("SET SESSION optimizer_switch='derived_merge=on'"))
            source = EnterpriseIdentitySource.create(
                name=f"Removal {unique}", provider=IdentitySourceProvider.FEISHU,
                tenant_id=f"removal-{unique}",
            )
            other_source = EnterpriseIdentitySource.create(
                name=f"Other {unique}", provider=IdentitySourceProvider.FEISHU,
                tenant_id=f"other-{unique}",
            )
            session.add_all([source, other_source])
            await session.flush()
            users = {}
            tokens = {}
            for name in ("removed", "another", "present", "builtin", "noncanonical", "multiple", "other"):
                mapped = None
                if name in {"builtin", "noncanonical"}:
                    mapped = User(
                        external_id=(
                            f"feishu:{source.tenant_id}:{name}"
                            if name == "builtin" else f"local-{name}-{unique}"
                        ),
                        display_name=name,
                        auth_provider=AuthProvider.BUILTIN if name == "builtin" else AuthProvider.OIDC,
                    )
                    session.add(mapped)
                    await session.flush()
                user = await upsert_external_user(
                    session, other_source if name == "other" else source,
                    external_user_id=name, display_name=name, email=None, user=mapped,
                )
                users[name] = user
                if name == "multiple":
                    session.add(UserExternalIdentity(
                        user_id=user.id, identity_provider=f"secondary-{unique}", external_subject=name,
                    ))
                family = secrets.token_hex(16)
                tokens[name] = []
                for revoked_at in (None, old_revocation):
                    refresh = UserRefreshToken(
                        user_id=user.id, token_hash=secrets.token_hex(32), token_family=family,
                        expires_at=now + timedelta(days=1), revoked_at=revoked_at,
                    )
                    oauth = OAuthRefreshToken(
                        user_id=user.id, token_hash=secrets.token_hex(32), token_family=family,
                        client_id="regression-client", scopes='["mcp"]',
                        expires_at=now + timedelta(days=1), revoked_at=revoked_at,
                    )
                    external = ExternalTokenSession(
                        user_id=user.id, token_family=family, provider_type="feishu",
                        provider_key=f"feishu:{other_source.tenant_id if name == 'other' else source.tenant_id}",
                        provider_fingerprint="test",
                        external_subject=name, subject_token_ciphertext="test",
                        last_validated_at=now, expires_at=now + timedelta(days=1), revoked_at=revoked_at,
                    )
                    tokens[name].append((refresh, oauth, external))
                    session.add_all([refresh, oauth, external])
            await session.flush()
            directories = (await session.scalars(select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id.in_([source.id, other_source.id]),
            ))).all()
            for directory in directories:
                directory.updated_at = now - timedelta(days=2)
                if directory.external_user_id in {"another", "other"}:
                    directory.status = EnterpriseDirectoryEntryStatus.DISABLED
            stale_group = await upsert_directory_group(
                session, source, external_group_id="stale-department", display_name="Stale",
                principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
            )
            stale_group.updated_at = now - timedelta(days=2)
            await session.commit()
            await session.refresh(stale_group)

            async def synchronize():
                if sync_mode == "streaming":
                    sink = _StreamingDirectorySink(session, source)
                    await sink.begin_sync(now.isoformat())
                    await sink.upsert_users([{"id": "present", "display_name": "present"}])
                    await sink.complete_sync()
                elif sync_mode == "snapshot":
                    await replace_directory_snapshot(
                        session, source, users=[{"id": "present", "display_name": "present"}],
                        groups=[], memberships=[],
                    )
                else:
                    for name in ("removed", "builtin", "noncanonical", "multiple"):
                        await apply_feishu_directory_event(
                            session, source, event_type="contact.user.deleted_v3", subject_id=name,
                        )
                await session.commit()

            for _ in range(2):
                await synchronize()
                if sync_mode == "streaming":
                    assert stale_group.status == EnterpriseDirectoryEntryStatus.DISABLED
                for name, user in users.items():
                    should_disable = name == "removed" or (name == "another" and sync_mode != "event")
                    await session.refresh(user)
                    assert user.status == (UserStatus.DISABLED if should_disable else UserStatus.ACTIVE), name
                    for index, family_tokens in enumerate(tokens[name]):
                        for token in family_tokens:
                            await session.refresh(token)
                            if index == 1:
                                assert token.revoked_at.replace(tzinfo=UTC) == old_revocation
                            else:
                                assert (token.revoked_at is not None) == should_disable, name
                for directory in directories:
                    await session.refresh(directory)
                    if directory.identity_source_id == source.id and directory.external_user_id == "removed":
                        assert directory.status == EnterpriseDirectoryEntryStatus.DISABLED
    finally:
        await engine.dispose()
