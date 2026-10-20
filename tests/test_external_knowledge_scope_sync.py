from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
import base64
import json
import os

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.external_knowledge_scope_sync import (
    ExternalKnowledgeScopeSyncError,
    ExternalKnowledgeScopeStaleSnapshotError,
    ImportedGrant,
    ImportedSnapshot,
    import_external_knowledge_scope_snapshot,
)
from server.api.external_knowledge_scopes import (
    AutoBindRequest,
    update_external_knowledge_auto_bind,
)
from server.enterprise_identity.service import identity_provider_key
from server.enterprise_identity.service import reconcile_feishu_groups, sync_identity_source
from server.enterprise_identity.acl_snapshot import (
    AclMembershipSnapshot,
    ExternalKnowledgeScopeGrant as PulledGrant,
    ExternalKnowledgeScopeSnapshot as PulledSnapshot,
)
from server.enterprise_identity.feishu import FeishuDirectorySnapshot
from server.core.crypto import encrypt
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeOrigin,
    AgentKnowledgeScopeMode,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AuthProvider,
    Base,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    ExternalKnowledgeScopeGrant,
    ExternalKnowledgeScopeSnapshot,
    IdentitySourceProvider,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
)


@pytest.fixture
async def seeded():
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
        member = User(
            external_id="member",
            display_name="Member",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add_all([admin, member])
        await session.flush()
        source = EnterpriseIdentitySource.create(
            name="Feishu",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-a",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        agent = Agent(
            name="agent",
            created_by=admin.id,
            knowledge_scope_mode=AgentKnowledgeScopeMode.SCOPED,
            external_knowledge_auto_bind_enabled=True,
        )
        instance = PolarRAGInstance(
            name="rag",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext="u",
            password_ciphertext="p",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add_all([source, agent, instance])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="space-a",
            name="Space",
            identity_domain="tenant-a",
            enabled=True,
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=instance.id,
            space_id=space.space_id,
            kb_id="kb-a",
            name="KB A",
            kb_type="PRIVATE",
            identity_domain="tenant-a",
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        )
        session.add_all(
            [
                resource,
                EnterpriseIdentitySourceSpaceBinding(
                    identity_source_id=source.id,
                    knowledge_space_id=space.knowledge_space_id,
                ),
                AgentPolarRAGInstanceBinding(
                    agent_id=agent.id,
                    polarrag_instance_id=instance.id,
                    created_by_user_id=admin.id,
                ),
                AgentUserAssignment(
                    agent_id=agent.id,
                    user_id=member.id,
                    created_by_user_id=admin.id,
                ),
                UserExternalIdentity(
                    user_id=member.id,
                    identity_provider=identity_provider_key(source),
                    external_subject="ou-member",
                ),
            ]
        )
        await session.commit()
        yield session, source, agent, member, resource
    await engine.dispose()


def _snapshot(
    status="ACTIVE",
    generation="g1",
    source_updated_at=datetime(2026, 9, 20, tzinfo=UTC),
):
    return ImportedSnapshot(
        snapshot_id="snapshot-a",
        tenant_key="tenant-a",
        source_scope_type="wiki_space",
        source_scope_id="wiki-a",
        source_scope_name="Engineering",
        space_id="space-a",
        kb_id="kb-a",
        generation=generation,
        fingerprint=generation,
        status=status,
        grants=(ImportedGrant("user", "ou-member"),),
        source_updated_at=source_updated_at,
    )


async def test_document_discovery_coexists_with_wiki_and_revokes_independently(seeded):
    from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope

    session, source, agent, member, resource = seeded
    document = replace(
        _snapshot(), snapshot_id="document-discovery", source_scope_type="document_read_discovery",
        source_scope_id="kb-a", source_scope_name="Readable Feishu documents",
    )
    await import_external_knowledge_scope_snapshot(
        session, identity_source_id=source.id, snapshot=document,
    )
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert scope.allows(resource)
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 1

    await import_external_knowledge_scope_snapshot(
        session, identity_source_id=source.id,
        snapshot=replace(document, generation="empty", fingerprint="empty", grants=(),
                         source_updated_at=datetime(2026, 9, 21, tzinfo=UTC)),
    )
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert not scope.allows(resource)

    await import_external_knowledge_scope_snapshot(
        session, identity_source_id=source.id, snapshot=_snapshot(),
    )
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert scope.allows(resource)


async def test_document_discovery_does_not_expand_agent_global_scope(seeded):
    session, source, agent, _member, _resource = seeded
    binding = await session.scalar(select(AgentPolarRAGInstanceBinding))
    binding.public_knowledge_resource_ids_json = "[]"
    # Resource is private; removing the instance itself is the global boundary.
    await session.delete(binding)
    await session.commit()
    result = await import_external_knowledge_scope_snapshot(
        session, identity_source_id=source.id,
        snapshot=replace(_snapshot(), snapshot_id="document-discovery",
                         source_scope_type="document_read_discovery"),
    )
    assert result["agents_projected"] == 0
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0


def test_read_only_and_import_routes_are_registered():
    from server.api.external_knowledge_scopes import router

    routes = {(method, route.path) for route in router.routes for method in getattr(route, "methods", set())}
    assert (
        "GET",
        "/admin/identity-sources/{source_id}/agents/{agent_id}/users/{external_user_id}/knowledge-resources",
    ) in routes
    assert ("GET", "/me/agents/{agent_id}/knowledge-resources") in routes
    assert (
        "PUT",
        "/admin/identity-sources/{source_id}/external-knowledge-scope-snapshots/{snapshot_id}",
    ) in routes


async def test_import_projects_only_to_opted_in_scoped_agent(seeded):
    session, source, agent, member, resource = seeded

    result = await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=_snapshot(),
    )
    await session.commit()

    assert result == {
        "snapshot_id": "snapshot-a",
        "agents_projected": 1,
        "bindings_projected": 1,
    }
    scope = (await session.execute(select(AgentKnowledgeScope))).scalar_one()
    assert scope.agent_id == agent.id
    assert scope.knowledge_resource_ids_json == f'["{resource.id}"]'
    binding = (await session.execute(select(AgentKnowledgeScopeBinding))).scalar_one()
    assert binding.user_id == member.id


async def test_identity_source_sync_pulls_and_projects_external_knowledge_scope(seeded, monkeypatch):
    session, source, agent, member, _resource = seeded
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source.config_ciphertext = encrypt(
        json.dumps(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "acl_membership_snapshot": {
                    "host": "meta.example.test",
                    "port": 5432,
                    "database": "polar_rag_meta",
                    "username": "readonly",
                    "password": "snapshot-secret",
                    "dialect": "postgresql",
                },
            }
        )
    )

    class FakeDirectoryClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[{"id": "ou-member", "display_name": "Member", "email": None}],
                groups=[],
                memberships=[],
            )

    class FakeAclClient:
        def __init__(self, **kwargs):
            assert kwargs["dialect"] == "postgresql"

        async def fetch_snapshot(self, *, tenant_id: str):
            assert tenant_id == "tenant-a"
            return AclMembershipSnapshot(
                groups=[],
                memberships=[],
                knowledge_scopes=(
                    PulledSnapshot(
                        snapshot_id="snapshot-pulled",
                        tenant_key="tenant-a",
                        source_scope_type="wiki_space",
                        source_scope_id="wiki-a",
                        source_scope_name="Engineering",
                        space_id="space-a",
                        kb_id="kb-a",
                        generation="g1",
                        fingerprint="f1",
                        status="ACTIVE",
                        source_updated_at=datetime.now(UTC),
                        grants=(PulledGrant(subject_type="user", subject_id="ou-member"),),
                    ),
                ),
            )

    await sync_identity_source(
        session,
        source,
        feishu_client_factory=FakeDirectoryClient,
        acl_snapshot_client_factory=FakeAclClient,
    )
    await session.commit()

    scope = (
        await session.execute(
            select(AgentKnowledgeScope).where(AgentKnowledgeScope.external_scope_id == "snapshot-pulled")
        )
    ).scalar_one()
    assert scope.agent_id == agent.id
    binding = (
        await session.execute(select(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id == scope.id))
    ).scalar_one()
    assert binding.user_id == member.id


async def test_import_is_idempotent_and_deny_all_removes_binding(seeded):
    session, source, _agent, _member, _resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=_snapshot(
            status="DENY_ALL",
            generation="g2",
            source_updated_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
    )
    with pytest.raises(
        ExternalKnowledgeScopeStaleSnapshotError,
        match="snapshot generation is stale or unordered",
    ):
        await import_external_knowledge_scope_snapshot(
            session,
            identity_source_id=source.id,
            snapshot=_snapshot(),
        )
    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=_snapshot(
            status="DENY_ALL",
            generation="g2",
            source_updated_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
    )
    await session.commit()

    assert await session.scalar(select(func.count(AgentKnowledgeScope.id))) == 1
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0
    assert await session.scalar(select(func.count(ExternalKnowledgeScopeGrant.id))) == 1
    stored = await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a")
    assert stored is not None
    assert (stored.generation, stored.status) == ("g2", "DENY_ALL")


async def test_cached_old_snapshot_cannot_restore_revoked_binding(seeded):
    session, source, _agent, _member, _resource = seeded
    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=_snapshot(),
    )
    await session.commit()

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async with factory() as stale_session:
        cached = await stale_session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a")
        assert cached is not None
        assert (cached.generation, cached.status) == ("g1", "ACTIVE")

        async with factory() as revoking_session:
            await import_external_knowledge_scope_snapshot(
                revoking_session,
                identity_source_id=source.id,
                snapshot=_snapshot(
                    status="DENY_ALL",
                    generation="g2",
                    source_updated_at=datetime(2026, 9, 21, tzinfo=UTC),
                ),
            )
            await revoking_session.commit()

        with pytest.raises(
            ExternalKnowledgeScopeStaleSnapshotError,
            match="snapshot generation is stale or unordered",
        ):
            await import_external_knowledge_scope_snapshot(
                stale_session,
                identity_source_id=source.id,
                snapshot=_snapshot(),
            )
        await stale_session.rollback()

    async with factory() as verification_session:
        stored = await verification_session.get(
            ExternalKnowledgeScopeSnapshot,
            "snapshot-a",
        )
        assert stored is not None
        assert (stored.generation, stored.status) == ("g2", "DENY_ALL")
        assert await verification_session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0


async def test_group_reconciliation_applies_scope_revocation(seeded, monkeypatch):
    session, source, _agent, _member, _resource = seeded
    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=_snapshot(),
    )
    await session.commit()
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 1

    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    source.config_ciphertext = encrypt(
        json.dumps(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "acl_membership_snapshot": {
                    "host": "meta.example.test",
                    "port": 5432,
                    "database": "polar_rag_meta",
                    "username": "readonly",
                    "password": "snapshot-secret",
                    "dialect": "postgresql",
                },
            }
        )
    )
    await session.commit()

    class FakeGroupClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_group_snapshot(self):
            return FeishuDirectorySnapshot(users=[], groups=[], memberships=[])

    pulled_scope = [
        PulledSnapshot(
            snapshot_id="snapshot-a",
            tenant_key=source.tenant_id,
            source_scope_type="wiki_space",
            source_scope_id="wiki-a",
            source_scope_name="Wiki A",
            space_id="space-a",
            kb_id="kb-a",
            generation="g2",
            fingerprint="f2",
            status="DENY_ALL",
            source_updated_at=datetime(2026, 9, 21, tzinfo=UTC),
            grants=(PulledGrant(subject_type="user", subject_id="external-member"),),
        )
    ]

    class FakeAclClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self, *, tenant_id: str):
            assert tenant_id == source.tenant_id
            return AclMembershipSnapshot(
                groups=[],
                memberships=[],
                knowledge_scopes=tuple(pulled_scope),
            )

    await reconcile_feishu_groups(
        session,
        source,
        feishu_client_factory=FakeGroupClient,
        acl_snapshot_client_factory=FakeAclClient,
    )

    stored = await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a")
    assert stored is not None
    assert (stored.generation, stored.status) == ("g2", "DENY_ALL")
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0

    pulled_scope[0] = replace(
        pulled_scope[0],
        generation="g1-replayed",
        fingerprint="f1-replayed",
        status="ACTIVE",
        source_updated_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    await reconcile_feishu_groups(
        session,
        source,
        feishu_client_factory=FakeGroupClient,
        acl_snapshot_client_factory=FakeAclClient,
    )

    await session.refresh(stored)
    assert (stored.generation, stored.status) == ("g2", "DENY_ALL")
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0


async def test_identity_sync_skips_unmapped_scope_and_removes_stale_projection(seeded, monkeypatch):
    session, source, _agent, _member, _resource = seeded
    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=replace(_snapshot(), snapshot_id="snapshot-unmapped"),
    )
    await session.commit()

    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source.config_ciphertext = encrypt(
        json.dumps(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "acl_membership_snapshot": {
                    "host": "meta.example.test",
                    "port": 5432,
                    "database": "polar_rag_meta",
                    "username": "readonly",
                    "password": "snapshot-secret",
                    "dialect": "postgresql",
                },
            }
        )
    )

    class FakeDirectoryClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return FeishuDirectorySnapshot(users=[], groups=[], memberships=[])

    class FakeAclClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self, *, tenant_id: str):
            return AclMembershipSnapshot(
                groups=[],
                memberships=[],
                knowledge_scopes=(
                    PulledSnapshot(
                        snapshot_id="snapshot-unmapped",
                        tenant_key=tenant_id,
                        source_scope_type="wiki_space",
                        source_scope_id="wiki-missing",
                        source_scope_name="Missing",
                        space_id="space-a",
                        kb_id="kb-missing",
                        generation="g2",
                        fingerprint="f2",
                        status="ACTIVE",
                        source_updated_at=datetime.now(UTC),
                        grants=(),
                    ),
                ),
            )

    await sync_identity_source(
        session,
        source,
        feishu_client_factory=FakeDirectoryClient,
        acl_snapshot_client_factory=FakeAclClient,
    )
    await session.commit()

    assert source.status == EnterpriseIdentitySourceStatus.ACTIVE
    assert await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-unmapped") is None
    assert (
        await session.scalar(
            select(func.count(AgentKnowledgeScope.id)).where(
                AgentKnowledgeScope.external_scope_id == "snapshot-unmapped"
            )
        )
        == 0
    )
    assert json.loads(source.sync_warning_json or "{}")["external_knowledge_scopes"] == {
        "code": "SCOPES_PARTIAL",
        "errors": {"knowledge resource not found": 1},
        "skipped_count": 1,
    }


async def test_enabling_auto_bind_reprojects_only_requested_agent(seeded):
    session, source, agent, member, _resource = seeded
    second_agent = Agent(
        name="second-agent",
        created_by=member.id,
        knowledge_scope_mode=AgentKnowledgeScopeMode.SCOPED,
        external_knowledge_auto_bind_enabled=True,
    )
    session.add(second_agent)
    await session.flush()
    instance_id = await session.scalar(select(PolarRAGInstance.id))
    assert instance_id is not None
    session.add_all(
        [
            AgentPolarRAGInstanceBinding(
                agent_id=second_agent.id,
                polarrag_instance_id=instance_id,
                created_by_user_id=member.id,
            ),
            AgentUserAssignment(
                agent_id=second_agent.id,
                user_id=member.id,
                created_by_user_id=member.id,
            ),
        ]
    )
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    second_scope = await session.scalar(
        select(AgentKnowledgeScope).where(AgentKnowledgeScope.agent_id == second_agent.id)
    )
    assert second_scope is not None
    second_scope.name = "sentinel"
    agent.external_knowledge_auto_bind_enabled = False
    await session.commit()

    await update_external_knowledge_auto_bind(
        agent.id,
        AutoBindRequest(enabled=True),
        _admin=member,
        session=session,
    )

    await session.refresh(second_scope)
    assert second_scope.name == "sentinel"


async def test_user_grants_resolve_agent_access_without_per_user_queries(seeded):
    session, source, agent, member, _resource = seeded
    grants = [ImportedGrant("user", "ou-member")]
    for index in range(25):
        user = User(
            external_id=f"member-{index}",
            display_name=f"Member {index}",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add(user)
        await session.flush()
        session.add_all(
            [
                AgentUserAssignment(
                    agent_id=agent.id,
                    user_id=user.id,
                    created_by_user_id=member.id,
                ),
                UserExternalIdentity(
                    user_id=user.id,
                    identity_provider=identity_provider_key(source),
                    external_subject=f"ou-{index}",
                ),
            ]
        )
        grants.append(ImportedGrant("user", f"ou-{index}"))
    await session.flush()

    select_count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    bind = session.bind
    assert bind is not None
    event.listen(bind.sync_engine, "before_cursor_execute", count_selects)
    try:
        await import_external_knowledge_scope_snapshot(
            session,
            identity_source_id=source.id,
            snapshot=replace(_snapshot(), snapshot_id="snapshot-batch", grants=tuple(grants)),
        )
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", count_selects)

    assert select_count < 30
    assert (
        await session.scalar(
            select(func.count(AgentKnowledgeScopeBinding.id)).where(AgentKnowledgeScopeBinding.user_id.is_not(None))
        )
        == 26
    )


async def test_batched_user_grants_preserve_identity_group_agent_access(seeded):
    session, source, agent, member, _resource = seeded
    await session.execute(
        delete(AgentUserAssignment).where(
            AgentUserAssignment.agent_id == agent.id,
            AgentUserAssignment.user_id == member.id,
        )
    )
    session.add_all(
        [
            EnterpriseDirectoryUser(
                identity_source_id=source.id,
                external_user_id="ou-member",
                display_name="Member",
                status=EnterpriseDirectoryEntryStatus.ACTIVE,
            ),
            EnterpriseDirectoryGroup(
                identity_source_id=source.id,
                external_group_id="od-engineering",
                display_name="Engineering",
                principal_type=EnterpriseDirectoryPrincipalType.GROUP,
                status=EnterpriseDirectoryEntryStatus.ACTIVE,
            ),
            EnterpriseDirectoryMembership(
                identity_source_id=source.id,
                external_group_id="od-engineering",
                member_type=EnterpriseDirectoryMembershipType.USER,
                external_member_id="ou-member",
            ),
            AgentGroupAssignment.for_identity_source_group(
                agent_id=agent.id,
                identity_source_id=source.id,
                external_group_id="od-engineering",
                created_by_user_id=member.id,
            ),
        ]
    )
    await session.flush()

    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=replace(_snapshot(), snapshot_id="snapshot-group-access"),
    )

    binding = await session.scalar(
        select(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.user_id == member.id)
    )
    assert binding is not None


async def test_resource_resolution_is_scoped_by_identity_source_binding(seeded):
    session, source, _agent, member, resource = seeded
    second_instance = PolarRAGInstance(
        name="rag-duplicate",
        scheme="https",
        host="rag-duplicate.example.test",
        port=9200,
        username_ciphertext="u",
        password_ciphertext="p",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=member.id,
    )
    session.add(second_instance)
    await session.flush()
    second_space = PolarRAGSpace(
        polarrag_instance_id=second_instance.id,
        space_id="space-a",
        name="Duplicate Space",
        identity_domain="tenant-a",
        enabled=True,
    )
    session.add(second_space)
    await session.flush()
    second_resource = KnowledgeResource(
        knowledge_space_id=second_space.knowledge_space_id,
        polarrag_instance_id=second_instance.id,
        space_id="space-a",
        kb_id="kb-a",
        name="Duplicate KB",
        kb_type="PRIVATE",
        identity_domain="tenant-a",
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
    )
    session.add(second_resource)
    await session.flush()

    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=replace(_snapshot(), snapshot_id="snapshot-scoped-target"),
    )
    stored = await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-scoped-target")
    assert stored is not None
    assert stored.knowledge_resource_id == resource.id

    session.add(
        EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id,
            knowledge_space_id=second_space.knowledge_space_id,
        )
    )
    await session.flush()
    with pytest.raises(
        ExternalKnowledgeScopeSyncError,
        match="knowledge resource target is ambiguous",
    ):
        await import_external_knowledge_scope_snapshot(
            session,
            identity_source_id=source.id,
            snapshot=replace(_snapshot(), snapshot_id="snapshot-ambiguous-target"),
        )


async def test_auto_bind_console_readback_requires_admin_and_tracks_updates(seeded):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from server.api.external_knowledge_scopes import router
    from server.auth.dependencies import get_current_user
    from server.db.engine import get_session
    from server.models import UserRole

    session, _source, agent, member, _resource = seeded
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def session_override():
        yield session

    app.dependency_overrides[get_session] = session_override
    path = f"/api/admin/agents/{agent.id}/external-knowledge-auto-bind"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: member
        assert (await client.get(path)).status_code == 403
        member.role = UserRole.ADMIN
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "agent_id": agent.id,
            "enabled": True,
            "knowledge_scope_mode": "SCOPED",
        }
        assert (await client.put(path, json={"enabled": False})).status_code == 200
        assert (await client.get(path)).json()["enabled"] is False
        assert (await client.get("/api/admin/agents/missing/external-knowledge-auto-bind")).status_code == 404


async def test_agent_reconcile_requires_scoped_auto_and_preserves_manual(seeded):
    from fastapi import HTTPException
    from server.api.external_knowledge_scopes import reproject_agent_external_knowledge
    session, source, agent, member, resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    manual = AgentKnowledgeScope(agent_id=agent.id, name="manual", origin=AgentKnowledgeScopeOrigin.MANUAL, knowledge_resource_ids_json=json.dumps([resource.id]), created_by_user_id=member.id)
    session.add(manual)
    await session.flush()
    manual_id = manual.id
    result = await reproject_agent_external_knowledge(session, agent)
    assert result["snapshots_processed"] == 1
    assert await session.get(AgentKnowledgeScope, manual_id) is not None
    agent.external_knowledge_auto_bind_enabled = False
    with pytest.raises(HTTPException) as exc:
        await reproject_agent_external_knowledge(session, agent)
    assert exc.value.status_code == 409
    agent.external_knowledge_auto_bind_enabled = True
    agent.knowledge_scope_mode = AgentKnowledgeScopeMode.LEGACY_ALL
    with pytest.raises(HTTPException):
        await reproject_agent_external_knowledge(session, agent)


async def test_independent_etl_stream_failure_rolls_back_imported_scopes(seeded, monkeypatch):
    from server.enterprise_identity import etl_permissions as etl
    session, source, agent, member, resource = seeded
    source.config_ciphertext = encrypt(json.dumps({"acl_membership_snapshot": {"host": "localhost", "port": 5432, "username": "readonly", "password": "secret", "dialect": "postgresql", "database": "meta"}}))
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    async def broken_scopes():
        yield PulledSnapshot(snapshot_id="snapshot-a", tenant_key="tenant-a", source_scope_type="wiki_space", source_scope_id="wiki-a", source_scope_name="Wiki", space_id="space-a", kb_id="kb-a", generation="g1", fingerprint="fingerprint-a", status="ACTIVE", source_updated_at=datetime.now(UTC), grants=(PulledGrant(subject_type="user", subject_id="ou-member"),))
        raise RuntimeError("read interrupted")
    class Client:
        def __init__(self, **kwargs):
            pass
        async def verify_connection(self, **kwargs):
            pass
        def iter_knowledge_scopes(self, **kwargs):
            return broken_scopes()
    owner = await etl.claim(session, source, "sync")
    await session.commit()
    await etl.run(source.id, owner, session_factory=factory, client_factory=Client)
    await session.refresh(source)
    assert source.status == EnterpriseIdentitySourceStatus.ACTIVE
    assert (await etl.status(session, source.id))["state"] == "FAILED"
    assert (await session.execute(select(ExternalKnowledgeScopeSnapshot))).scalars().all() == []


async def test_mode_round_trip_rebuilds_only_current_snapshot_grants(seeded, monkeypatch):
    from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope
    from server.api.agent_knowledge_scopes import KnowledgeScopeModeRequest, update_knowledge_scope_mode
    from server.models import AgentKnowledgePolicy

    session, source, agent, member, resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    before_clock = source.last_group_synced_at
    aid, rid, uid = agent.id, resource.id, member.id
    # Re-entering SCOPED must refresh grants, not resurrect an orphan derived scope.
    agent.knowledge_scope_mode = AgentKnowledgeScopeMode.LEGACY_ALL
    agent.external_knowledge_auto_bind_enabled = False
    orphan = AgentKnowledgeScope(agent_id=aid, name="Old derived scope", origin=AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
        identity_source_id=source.id, external_scope_id="orphan", knowledge_resource_ids_json=json.dumps([rid]))
    session.add(orphan)
    await session.flush()
    session.add(AgentKnowledgeScopeBinding.for_user(scope_id=orphan.id, user_id=uid))
    session.add(AgentKnowledgePolicy(agent_id=aid, knowledge_resource_id=rid, select_all=True, excluded_subjects_json="[]"))
    await session.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("Mode switching must not open an upstream DB connection")
    monkeypatch.setattr("server.enterprise_identity.acl_snapshot.FeishuAclMembershipSnapshotClient.fetch_snapshot", forbidden)
    result = await update_knowledge_scope_mode(aid, KnowledgeScopeModeRequest(mode="SCOPED",
        expected_mode="LEGACY_ALL", external_knowledge_auto_bind_enabled=True, reset_all_users_policies=True), member, session)
    assert result["removed_external_scopes"] == 2
    assert result["removed_all_users_policies"] == 1
    assert (await resolve_agent_knowledge_resource_scope(session, aid, uid)).allows(resource)
    assert source.last_group_synced_at == before_clock
    # The mode API can also disable auto-binding without changing SCOPED.
    await update_knowledge_scope_mode(aid, KnowledgeScopeModeRequest(mode="SCOPED",
        expected_mode="SCOPED", external_knowledge_auto_bind_enabled=False), member, session)
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0
    await update_knowledge_scope_mode(aid, KnowledgeScopeModeRequest(mode="SCOPED",
        expected_mode="SCOPED", external_knowledge_auto_bind_enabled=True), member, session)
    assert await session.scalar(select(func.count(AgentKnowledgeScope.id)).where(AgentKnowledgeScope.agent_id == aid)) == 1
    await update_knowledge_scope_mode(aid, KnowledgeScopeModeRequest(mode="LEGACY_ALL", expected_mode="SCOPED"), member, session)
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 0
    assert not agent.external_knowledge_auto_bind_enabled
    await update_knowledge_scope_mode(aid, KnowledgeScopeModeRequest(mode="SCOPED", expected_mode="LEGACY_ALL",
        external_knowledge_auto_bind_enabled=True), member, session)
    assert (await resolve_agent_knowledge_resource_scope(session, aid, uid)).allows(resource)


async def test_failed_mode_reprojection_keeps_previous_mode_and_grants(seeded, monkeypatch):
    from server.api.agent_knowledge_scopes import KnowledgeScopeModeRequest, update_knowledge_scope_mode
    from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope
    from fastapi import HTTPException
    session, source, agent, member, resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    aid, uid, rid = agent.id, member.id, resource.id
    async def fail(*args):
        raise ExternalKnowledgeScopeSyncError("invalid snapshot")
    monkeypatch.setattr("server.api.external_knowledge_scopes.reproject_agent_external_knowledge", fail)
    with pytest.raises(HTTPException) as exc:
        await update_knowledge_scope_mode(aid, KnowledgeScopeModeRequest(mode="SCOPED", reset_all_users_policies=True), member, session)
    assert exc.value.status_code == 409
    assert await session.scalar(select(func.count(AgentKnowledgeScopeBinding.id))) == 1
    assert (await session.get(Agent, aid)).external_knowledge_auto_bind_enabled
    assert (await resolve_agent_knowledge_resource_scope(session, aid, uid)).allows(
        await session.get(KnowledgeResource, rid)
    )
