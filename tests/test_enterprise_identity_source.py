from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import base64
import json
import os

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.enterprise_identity.service import (
    _StreamingDirectorySink,
    _feishu_sync_concurrency,
    upsert_directory_group,
    upsert_directory_membership,
    upsert_external_user,
    replace_directory_snapshot,
    sync_identity_source,
)
from server.enterprise_identity.sync import (
    _retry_delay_seconds,
    identity_source_sync_loop,
    schedule_identity_source_sync,
    sync_configured_identity_sources_once,
    sync_task,
)
from server.models import (
    AuthProvider,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryUser,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceSpaceBinding,
    IdentitySourceProvider,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    Base,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    User,
    UserExternalIdentity,
)
from server.polarrag.identity import (
    IdentityContextUnavailable,
    resolve_acl_context,
)
from server.polarrag.access import list_visible_knowledge_resources, plan_knowledge_access
from server.core.crypto import encrypt
from server.enterprise_identity.feishu import FeishuDirectoryClient, FeishuDirectorySnapshot
from server.enterprise_identity.sharepoint import SharePointDirectorySnapshot


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _source(session, tenant_id: str = "tenant-001") -> EnterpriseIdentitySource:
    source = EnterpriseIdentitySource.create(
        name=f"Feishu engineering {tenant_id}",
        provider=IdentitySourceProvider.FEISHU,
        tenant_id=tenant_id,
    )
    session.add(source)
    await session.flush()
    return source


@pytest.mark.asyncio
async def test_feishu_directory_and_jit_share_tenant_scoped_external_identity(session):
    source = await _source(session)

    synced = await upsert_external_user(
        session,
        source,
        external_user_id="ou_123",
        display_name="Alice",
        email="alice@example.com",
    )
    jitted = await upsert_external_user(
        session,
        source,
        external_user_id="ou_123",
        display_name="Alice renamed",
        email=None,
    )

    assert synced.id == jitted.id
    assert jitted.external_id == "feishu:tenant-001:ou_123"
    assert jitted.password_hash is None
    assert jitted.display_name == "Alice renamed"


async def test_background_sync_uses_configured_sharepoint_adapter(
    session, monkeypatch
) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = EnterpriseIdentitySource.create(
        name="SharePoint engineering",
        provider=IdentitySourceProvider.SHAREPOINT,
        tenant_id="tenant-001",
    )
    source.config_ciphertext = encrypt(
        json.dumps(
            {"cloud": "global", "client_id": "client-id", "client_secret": "client-secret"}
        )
    )
    session.add(source)
    await session.commit()

    class FakeClient:
        def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, cloud: str):
            assert (tenant_id, client_id, client_secret, cloud) == (
                "tenant-001", "client-id", "client-secret", "global"
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return SharePointDirectorySnapshot(
                users=[{"id": "entra-user-1", "display_name": "Alice", "email": None}],
                groups=[{"id": "entra-group-1", "display_name": "Engineering"}],
                memberships=[
                    {"group_id": "entra-group-1", "member_type": "user", "member_id": "entra-user-1"}
                ],
            )

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(
        factory, sharepoint_client_factory=FakeClient
    )

    async with factory() as refreshed:
        current = await refreshed.get(EnterpriseIdentitySource, source.id)
        assert current is not None
        assert current.status.value == "active"
        assert current.last_error is None
        directory_user = (
            await refreshed.execute(
                select(EnterpriseDirectoryUser).where(
                    EnterpriseDirectoryUser.identity_source_id == source.id
                )
            )
        ).scalar_one()
        assert directory_user.external_user_id == "entra-user-1"


async def test_streaming_identity_sync_records_completion_time(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session)
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app-id", "app_secret": "app-secret"}))
    await session.commit()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def sync_to_sink(self, sink):
            await sink.begin_sync(datetime.now(UTC).isoformat())
            await sink.upsert_users([
                {"id": "user-1", "display_name": "Alice", "email": "alice@example.test"}
            ])
            await sink.upsert_groups([
                {"id": "group-1", "display_name": "Engineering", "principal_type": "group"}
            ])
            await sink.upsert_memberships([
                {"group_id": "group-1", "member_type": "user", "member_id": "user-1"}
            ])
            await sink.complete_sync()

        async def clear_checkpoint(self):
            return None

    monkeypatch.setattr("server.enterprise_identity.feishu.FeishuDirectoryClient", lambda **_kwargs: FakeClient())

    await sync_identity_source(session, source)

    assert source.status.value == "active"
    assert source.last_synced_at is not None


async def test_streaming_membership_page_uses_set_based_upsert(
    session,
    monkeypatch,
) -> None:
    source = await _source(session)
    await upsert_external_user(
        session,
        source,
        external_user_id="user-1",
        display_name="User 1",
        email=None,
    )
    await upsert_external_user(
        session,
        source,
        external_user_id="user-2",
        display_name="User 2",
        email=None,
    )
    await upsert_directory_group(
        session,
        source,
        external_group_id="group-1",
        display_name="Group 1",
    )
    await session.commit()

    async def reject_row_upsert(*_args, **_kwargs):
        raise AssertionError("membership pages must not use row-by-row upserts")

    monkeypatch.setattr(
        "server.enterprise_identity.service.upsert_directory_membership",
        reject_row_upsert,
    )
    sink = _StreamingDirectorySink(session, source)
    await sink.upsert_memberships(
        [
            {"group_id": "group-1", "member_type": "user", "member_id": "user-1"},
            {"group_id": "group-1", "member_type": "user", "member_id": "user-2"},
        ]
    )

    rows = list((await session.execute(select(EnterpriseDirectoryMembership))).scalars())
    assert {row.external_member_id for row in rows} == {"user-1", "user-2"}


@pytest.mark.asyncio
async def test_streaming_identity_sync_rejects_pages_after_worker_takeover(
    session, monkeypatch
) -> None:
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    source = await _source(session)
    source.config_ciphertext = encrypt(
        json.dumps({"app_id": "app-id", "app_secret": "app-secret"})
    )
    source.sync_worker_id = "old-worker"
    await session.commit()
    source_id = source.id
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def sync_to_sink(self, sink):
            await sink.begin_sync(datetime.now(UTC).isoformat())
            async with factory() as takeover_session:
                claimed = await takeover_session.get(
                    EnterpriseIdentitySource, source_id
                )
                assert claimed is not None
                claimed.sync_worker_id = "new-worker"
                await takeover_session.commit()
            await sink.upsert_users(
                [{"id": "late-user", "display_name": "Late user"}]
            )

    monkeypatch.setattr(
        "server.enterprise_identity.feishu.FeishuDirectoryClient",
        lambda **_kwargs: FakeClient(),
    )

    with pytest.raises(RuntimeError, match="sync ownership was lost"):
        await sync_identity_source(session, source)
    await session.rollback()

    async with factory() as verification_session:
        assert await verification_session.scalar(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source_id,
                EnterpriseDirectoryUser.external_user_id == "late-user",
            )
        ) is None
        claimed = await verification_session.get(
            EnterpriseIdentitySource, source_id
        )
        assert claimed is not None
        assert claimed.sync_worker_id == "new-worker"


@pytest.mark.asyncio
async def test_feishu_streaming_sync_preserves_memberships_for_unavailable_user(session) -> None:
    source = await _source(session)
    user = await upsert_external_user(
        session,
        source,
        external_user_id="u-unavailable",
        display_name="Unavailable",
        email=None,
    )
    await upsert_external_user(
        session,
        source,
        external_user_id="u-ok",
        display_name="Available",
        email=None,
    )
    group = await upsert_directory_group(
        session,
        source,
        external_group_id="g-existing",
        display_name="Existing",
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id=group.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="u-unavailable",
    )
    await session.commit()

    fail_membership = True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path == "/open-apis/contact/v3/users":
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/users/find_by_department":
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"user_id": "u-ok", "name": "Available"},
                {"user_id": "u-unavailable", "name": "Unavailable"},
            ], "has_more": False}})
        if request.url.path.startswith("/open-apis/contact/v3/departments/"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/simplelist":
            return httpx.Response(200, json={"code": 0, "data": {"grouplist": [
                {"group_id": "g-existing", "name": "Existing"}
            ], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            if fail_membership and request.url.params["member_id"] == "u-unavailable":
                return httpx.Response(400, json={"code": 99991672}, request=request)
            return httpx.Response(200, json={"code": 0, "data": {"group_list": [], "has_more": False}})
        raise AssertionError(request.url)

    async with FeishuDirectoryClient(
        app_id="app",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
        max_concurrency=1,
    ) as client:
        await client.sync_to_sink(_StreamingDirectorySink(session, source))

    memberships = list(
        (await session.execute(select(EnterpriseDirectoryMembership))).scalars()
    )
    assert [(row.external_member_id, row.external_group_id) for row in memberships] == [
        (user.external_id.rsplit(":", 1)[-1], "g-existing")
    ]
    await session.refresh(source)
    assert json.loads(source.sync_warning_json or "{}") == {
        "code": "MEMBERSHIPS_PARTIAL",
        "provider_errors": {"99991672": 1},
        "skipped_count": 1,
    }

    fail_membership = False
    async with FeishuDirectoryClient(
        app_id="app",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
        max_concurrency=1,
    ) as client:
        await client.sync_to_sink(_StreamingDirectorySink(session, source))

    await session.refresh(source)
    assert source.sync_warning_json is None


@pytest.mark.asyncio
async def test_acl_context_projects_only_the_source_bound_to_target_space(session):
    source = await _source(session)
    source.status = "active"
    user = await upsert_external_user(
        session,
        source,
        external_user_id="ou_alice",
        display_name="Alice",
        email=None,
    )
    group = await upsert_directory_group(
        session,
        source,
        external_group_id="oc_engineering",
        display_name="Engineering chat",
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id=group.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="ou_alice",
    )
    instance = PolarRAGInstance(
        name="rag-a",
        scheme="https",
        host="rag.example.test",
        port=443,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=user.id,
    )
    session.add(instance)
    await session.flush()
    bound_space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-a",
        name="A",
        identity_domain="domain-a",
        enabled=True,
    )
    unbound_space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-b",
        name="B",
        identity_domain="domain-a",
        enabled=True,
    )
    session.add_all([bound_space, unbound_space])
    await session.flush()
    session.add(
        EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id,
            knowledge_space_id=bound_space.knowledge_space_id,
        )
    )
    await session.commit()

    context = await resolve_acl_context(session, user.id, bound_space.knowledge_space_id)

    assert context == {
        "identity_domain": "domain-a",
        "principals": [
            {"provider": "feishu", "type": "group", "id": "oc_engineering"},
            {"provider": "feishu", "type": "user", "id": "ou_alice"},
            {"provider": "polarrag", "type": "user", "id": user.external_id},
        ],
    }
    with pytest.raises(IdentityContextUnavailable):
        await resolve_acl_context(session, user.id, unbound_space.knowledge_space_id)


@pytest.mark.asyncio
async def test_source_tenant_is_part_of_the_external_identity_key(session):
    first_source = await _source(session, tenant_id="tenant-a")
    second_source = await _source(session, tenant_id="tenant-b")

    first = await upsert_external_user(
        session, first_source, external_user_id="ou_same", display_name="A", email=None
    )
    second = await upsert_external_user(
        session, second_source, external_user_id="ou_same", display_name="B", email=None
    )

    assert first.id != second.id
    assert {first.external_id, second.external_id} == {
        "feishu:tenant-a:ou_same",
        "feishu:tenant-b:ou_same",
    }


@pytest.mark.asyncio
async def test_public_resource_visibility_uses_the_target_space_binding(session):
    source = await _source(session)
    source.status = "active"
    user = await upsert_external_user(
        session, source, external_user_id="ou_alice", display_name="Alice", email=None
    )
    instance = PolarRAGInstance(
        name="rag-visibility",
        scheme="https",
        host="rag.example.test",
        port=443,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=user.id,
    )
    session.add(instance)
    await session.flush()
    bound_space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-bound",
        name="Bound",
        identity_domain="shared-domain",
        enabled=True,
    )
    unbound_space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-unbound",
        name="Unbound",
        identity_domain="shared-domain",
        enabled=True,
    )
    session.add_all([bound_space, unbound_space])
    await session.flush()
    session.add(
        EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id,
            knowledge_space_id=bound_space.knowledge_space_id,
        )
    )
    bound_resource = KnowledgeResource(
        knowledge_space_id=bound_space.knowledge_space_id,
        polarrag_instance_id=instance.id,
        space_id=bound_space.space_id,
        kb_id="kb-bound",
        name="Bound KB",
        kb_type="PUBLIC",
        identity_domain=bound_space.identity_domain,
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    unbound_resource = KnowledgeResource(
        knowledge_space_id=unbound_space.knowledge_space_id,
        polarrag_instance_id=instance.id,
        space_id=unbound_space.space_id,
        kb_id="kb-unbound",
        name="Unbound KB",
        kb_type="PUBLIC",
        identity_domain=unbound_space.identity_domain,
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add_all([bound_resource, unbound_resource])
    # SQLite returns DateTime values without tzinfo even when the model declares
    # timezone=True. Runtime ACL resolution must accept that persisted value.
    source.last_synced_at = datetime.now()
    await session.commit()

    visible = await list_visible_knowledge_resources(session, user)
    plan = await plan_knowledge_access(session, user, [bound_resource.id])

    assert [resource.id for resource in visible] == [bound_resource.id]
    assert plan.acl_context["identity_domain"] == "shared-domain"

    binding = (
        await session.execute(
            select(EnterpriseIdentitySourceSpaceBinding).where(
                EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id
                == bound_space.knowledge_space_id,
            )
        )
    ).scalar_one()
    await session.delete(binding)
    await session.commit()

    assert await list_visible_knowledge_resources(session, user) == []
    with pytest.raises(IdentityContextUnavailable):
        await resolve_acl_context(session, user.id, bound_space.knowledge_space_id)


@pytest.mark.asyncio
async def test_manual_identity_mapping_keeps_personal_owner_access(session):
    source = await _source(session)
    source.status = "active"
    source_user = await upsert_external_user(
        session,
        source,
        external_user_id="ou_alice",
        display_name="Alice",
        email=None,
    )
    pas_user = User(
        external_id="xiaoyuan",
        display_name="Xiaoyuan",
        auth_provider=AuthProvider.BUILTIN,
    )
    unrelated_user = User(
        external_id="unrelated",
        display_name="Unrelated",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add_all([pas_user, unrelated_user])
    await session.flush()
    identity = (
        await session.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == source_user.id
            )
        )
    ).scalar_one()
    identity.user_id = pas_user.id
    instance = PolarRAGInstance(
        name="rag-personal-owner",
        scheme="https",
        host="rag.example.test",
        port=443,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=pas_user.id,
    )
    session.add(instance)
    await session.flush()
    space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-personal-owner",
        name="Personal owner",
        identity_domain="domain-personal-owner",
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
    resource = KnowledgeResource(
        knowledge_space_id=space.knowledge_space_id,
        polarrag_instance_id=instance.id,
        space_id=space.space_id,
        kb_id="personal-owner",
        name="Personal owner",
        kb_type="PERSONAL",
        identity_domain=space.identity_domain,
        binding_mode=KnowledgeBindingMode.OWNER,
        owner_pas_user_id=source_user.id,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add(resource)
    await session.commit()

    assert [item.id for item in await list_visible_knowledge_resources(session, pas_user)] == [resource.id]
    plan = await plan_knowledge_access(session, pas_user, [resource.id])
    assert plan.acl_context["principals"] == [
        {"provider": "feishu", "type": "user", "id": "ou_alice"},
        {"provider": "polarrag", "type": "user", "id": source_user.external_id},
        {"provider": "polarrag", "type": "user", "id": pas_user.external_id},
    ]
    assert await list_visible_knowledge_resources(session, unrelated_user) == []


@pytest.mark.asyncio
async def test_directory_sync_preserves_manually_mapped_pas_user_profile(session):
    source = await _source(session)
    source.status = "active"
    source_user = await upsert_external_user(
        session,
        source,
        external_user_id="ou_weng",
        display_name="翁小院",
        email="weng@example.com",
    )
    pas_user = User(
        external_id="xiaoyuan",
        display_name="xiaoyuan",
        email="xiaoyuan@example.com",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(pas_user)
    await session.flush()
    identity = (
        await session.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == source_user.id
            )
        )
    ).scalar_one()
    identity.user_id = pas_user.id
    await session.commit()

    await replace_directory_snapshot(
        session,
        source,
        users=[
            {
                "id": "ou_weng",
                "display_name": "翁小院（已更新）",
                "email": "weng-updated@example.com",
            }
        ],
        groups=[],
        memberships=[],
    )
    await session.commit()

    await session.refresh(pas_user)
    directory_user = (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == "ou_weng",
            )
        )
    ).scalar_one()
    assert pas_user.display_name == "xiaoyuan"
    assert pas_user.email == "xiaoyuan@example.com"
    assert directory_user.display_name == "翁小院（已更新）"
    assert directory_user.email == "weng-updated@example.com"


@pytest.mark.asyncio
async def test_directory_snapshot_revokes_removed_group_membership(session):
    source = await _source(session)
    source.status = "active"
    user = await upsert_external_user(
        session, source, external_user_id="ou_alice", display_name="Alice", email=None
    )
    group = await upsert_directory_group(
        session, source, external_group_id="oc_engineering", display_name="Engineering"
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id=group.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="ou_alice",
    )
    instance = PolarRAGInstance(
        name="rag-revoke",
        scheme="https",
        host="rag.example.test",
        port=443,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=user.id,
    )
    session.add(instance)
    await session.flush()
    space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-revoke",
        name="Revoke",
        identity_domain="domain-revoke",
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

    await replace_directory_snapshot(
        session,
        source,
        users=[{"id": "ou_alice", "display_name": "Alice", "email": None}],
        groups=[{"id": "oc_engineering", "display_name": "Engineering"}],
        memberships=[],
        now=datetime.now(UTC),
    )
    await session.commit()

    context = await resolve_acl_context(session, user.id, space.knowledge_space_id)

    assert context["principals"] == [
        {"provider": "feishu", "type": "user", "id": "ou_alice"},
        {"provider": "polarrag", "type": "user", "id": user.external_id},
    ]


@pytest.mark.asyncio
async def test_stale_source_uses_last_successful_directory_snapshot(session):
    source = await _source(session)
    source.status = "active"
    source.last_synced_at = datetime.now(UTC) - timedelta(
        seconds=source.stale_after_seconds - 1
    )
    user = await upsert_external_user(
        session, source, external_user_id="ou_alice", display_name="Alice", email=None
    )
    instance = PolarRAGInstance(
        name="rag-stale",
        scheme="https",
        host="rag.example.test",
        port=443,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=user.id,
    )
    session.add(instance)
    await session.flush()
    space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-stale",
        name="Stale",
        identity_domain="domain-stale",
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

    source.status = "stale"
    await session.commit()

    context = await resolve_acl_context(session, user.id, space.knowledge_space_id)

    assert context["principals"] == [
        {"provider": "feishu", "type": "user", "id": "ou_alice"},
        {"provider": "polarrag", "type": "user", "id": user.external_id},
    ]

    source.last_synced_at = datetime.now(UTC) - timedelta(
        seconds=source.stale_after_seconds + 1
    )
    await session.commit()
    with pytest.raises(IdentityContextUnavailable):
        await resolve_acl_context(session, user.id, space.knowledge_space_id)


@pytest.mark.asyncio
async def test_feishu_parent_department_acl_includes_child_department_user(session):
    source = await _source(session)
    source.status = "active"
    user = await upsert_external_user(
        session, source, external_user_id="ou_alice", display_name="Alice", email=None
    )
    research = await upsert_directory_group(
        session,
        source,
        external_group_id="od_research",
        display_name="Research",
        principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
    )
    platform = await upsert_directory_group(
        session,
        source,
        external_group_id="od_platform",
        display_name="Platform",
        principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id=platform.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="ou_alice",
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id=research.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.GROUP,
        external_member_id=platform.external_group_id,
    )
    instance = PolarRAGInstance(
        name="rag-department",
        scheme="https",
        host="rag.example.test",
        port=443,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=user.id,
    )
    session.add(instance)
    await session.flush()
    space = PolarRAGSpace(
        polarrag_instance_id=instance.id,
        space_id="space-department",
        name="Department",
        identity_domain="domain-department",
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

    context = await resolve_acl_context(session, user.id, space.knowledge_space_id)

    assert {tuple(principal.values()) for principal in context["principals"]} >= {
        ("feishu", "department", "od_research"),
    }


@pytest.mark.asyncio
async def test_feishu_sync_decrypts_source_configuration_and_replaces_directory(session, monkeypatch):
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session)
    source.config_ciphertext = encrypt(
        json.dumps(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "acl_membership_snapshot": {
                    "host": "meta.example.test",
                    "port": 3306,
                    "username": "readonly",
                    "password": "snapshot-secret",
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, *, app_id: str, app_secret: str):
            assert (app_id, app_secret) == ("cli_test", "secret")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[{"id": "ou_alice", "display_name": "Alice", "email": None}],
                groups=[{"id": "g_engineering", "display_name": "Engineering", "principal_type": "group"}],
                memberships=[{"group_id": "g_engineering", "member_type": "user", "member_id": "ou_alice"}],
            )

    class FakeAclSnapshotClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self, *, tenant_id: str):
            assert tenant_id == "tenant-001"
            return type("Snapshot", (), {"groups": [], "memberships": []})()

    await sync_identity_source(
        session,
        source,
        feishu_client_factory=FakeClient,
        acl_snapshot_client_factory=FakeAclSnapshotClient,
    )

    assert source.status.value == "active"
    assert source.last_synced_at is not None


@pytest.mark.asyncio
async def test_feishu_sync_uses_directory_when_acl_membership_snapshot_is_not_configured(
    session, monkeypatch
):
    """A tenant-verified source must not require the optional ETL snapshot."""
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session)
    source.config_ciphertext = encrypt(
        json.dumps({"app_id": "cli_test", "app_secret": "secret"})
    )

    class FakeClient:
        def __init__(self, *, app_id: str, app_secret: str):
            assert (app_id, app_secret) == ("cli_test", "secret")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[{"id": "ou_alice", "display_name": "Alice", "email": None}],
                groups=[{"id": "od_engineering", "display_name": "Engineering", "principal_type": "department"}],
                memberships=[{"group_id": "od_engineering", "member_type": "user", "member_id": "ou_alice"}],
            )

    await sync_identity_source(session, source, feishu_client_factory=FakeClient)

    assert source.status.value == "active"
    assert source.last_synced_at is not None
    assert (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source.id
            )
        )
    ).scalar_one().external_user_id == "ou_alice"
    assert (
        await session.execute(
            select(EnterpriseDirectoryGroup).where(
                EnterpriseDirectoryGroup.identity_source_id == source.id
            )
        )
    ).scalar_one().principal_type == EnterpriseDirectoryPrincipalType.DEPARTMENT


@pytest.mark.asyncio
async def test_feishu_sync_merges_etl_acl_membership_snapshot(session, monkeypatch):
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session)
    source.config_ciphertext = encrypt(
        json.dumps(
            {
                "app_id": "cli_test",
                "app_secret": "secret",
                "acl_membership_snapshot": {
                    "host": "meta.example.test",
                    "port": 3306,
                    "username": "readonly",
                    "password": "snapshot-secret",
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[{"id": "ou_alice", "display_name": "Alice", "email": None}],
                groups=[{"id": "g_product", "display_name": "Product", "principal_type": "group"}],
                memberships=[{"group_id": "g_product", "member_type": "user", "member_id": "ou_alice"}],
            )

    class FakeAclSnapshotClient:
        def __init__(self, **kwargs):
            assert kwargs == {
                "host": "meta.example.test",
                "port": 3306,
                "username": "readonly",
                "password": "snapshot-secret",
                "database": "polar_rag_meta",
            }

        async def fetch_snapshot(self, *, tenant_id: str):
            assert tenant_id == "tenant-001"
            return type(
                "Snapshot",
                (),
                {
                    "groups": [
                        {"id": "od_research", "display_name": "Research", "principal_type": "department"},
                        {"id": "oc_chat", "display_name": "Chat", "principal_type": "group"},
                        {"id": "acl_group-1", "display_name": "ACL group", "principal_type": "acl_group"},
                    ],
                    "memberships": [
                        {"group_id": "od_research", "member_type": "user", "member_id": "ou_alice"},
                        {"group_id": "oc_chat", "member_type": "user", "member_id": "ou_alice"},
                        {"group_id": "acl_group-1", "member_type": "user", "member_id": "ou_alice"},
                    ],
                },
            )()

    await sync_identity_source(
        session,
        source,
        feishu_client_factory=FakeClient,
        acl_snapshot_client_factory=FakeAclSnapshotClient,
    )

    groups = list(
        (
            await session.execute(
                select(EnterpriseDirectoryGroup)
                .where(EnterpriseDirectoryGroup.identity_source_id == source.id)
                .order_by(EnterpriseDirectoryGroup.external_group_id)
            )
        ).scalars()
    )
    assert [(group.external_group_id, group.principal_type.value) for group in groups] == [
        ("acl_group-1", "acl_group"),
        ("g_product", "group"),
        ("oc_chat", "group"),
        ("od_research", "department"),
    ]


@pytest.mark.asyncio
async def test_background_sync_isolates_a_failed_source_and_commits_the_others(
    monkeypatch,
):
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        good = await _source(database_session, tenant_id="tenant-good")
        good.config_ciphertext = encrypt(
            json.dumps(
                {
                    "app_id": "good",
                    "app_secret": "secret",
                    "acl_membership_snapshot": {
                        "host": "meta.example.test",
                        "port": 3306,
                        "username": "readonly",
                        "password": "snapshot-secret",
                    },
                }
            )
        )
        bad = await _source(database_session, tenant_id="tenant-bad")
        bad.config_ciphertext = encrypt(
            json.dumps(
                {
                    "app_id": "bad",
                    "app_secret": "secret",
                    "acl_membership_snapshot": {
                        "host": "meta.example.test",
                        "port": 3306,
                        "username": "readonly",
                        "password": "snapshot-secret",
                    },
                }
            )
        )
        await database_session.commit()
        good_id, bad_id = good.id, bad.id

    class FakeClient:
        def __init__(self, *, app_id: str, app_secret: str):
            self.app_id = app_id

        async def __aenter__(self):
            if self.app_id == "bad":
                raise RuntimeError("source unavailable")
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[{"id": "ou_alice", "display_name": "Alice", "email": None}],
                groups=[],
                memberships=[],
            )

    class FakeAclSnapshotClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self, *, tenant_id: str):
            return type("Snapshot", (), {"groups": [], "memberships": []})()

    await sync_configured_identity_sources_once(
        factory,
        feishu_client_factory=FakeClient,
        acl_snapshot_client_factory=FakeAclSnapshotClient,
    )

    async with factory() as database_session:
        good = await database_session.get(EnterpriseIdentitySource, good_id)
        bad = await database_session.get(EnterpriseIdentitySource, bad_id)
        assert good is not None and good.status.value == "active"
        assert good.last_synced_at is not None
        assert bad is not None and bad.status.value == "stale"
        assert bad.last_error == "RuntimeError"
    await engine.dispose()


@pytest.mark.asyncio
async def test_identity_source_sync_loop_defaults_to_thirty_minutes(monkeypatch):
    observed_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        observed_delays.append(delay)
        raise asyncio.CancelledError

    async def fake_sync(_session_factory, **_kwargs) -> bool:
        return False

    async def fake_next_delay(
        _session_factory,
        configured_interval: float,
        **_kwargs,
    ) -> float:
        return configured_interval

    monkeypatch.setattr("server.enterprise_identity.sync.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("server.enterprise_identity.sync.sync_configured_identity_sources_once", fake_sync)
    monkeypatch.setattr(
        "server.enterprise_identity.sync._next_identity_source_sync_delay",
        fake_next_delay,
    )

    with pytest.raises(asyncio.CancelledError):
        await identity_source_sync_loop(object())

    assert observed_delays == [1800.0]


@pytest.mark.asyncio
async def test_identity_source_sync_loop_wakes_at_persisted_retry(
    session,
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    source = await _source(session, "tenant-persisted-retry")
    source.config_ciphertext = "configured"
    source.status = EnterpriseIdentitySourceStatus.STALE
    source.sync_next_retry_at = fixed_now + timedelta(seconds=60)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    observed_delays: list[float] = []

    async def fake_sync(_session_factory, **_kwargs) -> bool:
        return False

    async def fake_sleep(delay: float) -> None:
        observed_delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("server.enterprise_identity.sync.datetime", FrozenDateTime)
    monkeypatch.setattr(
        "server.enterprise_identity.sync.sync_configured_identity_sources_once",
        fake_sync,
    )
    monkeypatch.setattr("server.enterprise_identity.sync.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await identity_source_sync_loop(factory, interval_seconds=1800)

    assert observed_delays == [60.0]


@pytest.mark.asyncio
async def test_retry_deadline_crossed_during_cycle_wakes_next_pass(
    session,
    monkeypatch,
) -> None:
    selection_time = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
    delay_time = selection_time + timedelta(seconds=2)
    source = await _source(session, "tenant-crossed-retry")
    source.config_ciphertext = "configured"
    source.status = EnterpriseIdentitySourceStatus.STALE
    source.sync_next_retry_at = selection_time + timedelta(seconds=1)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    class FrozenDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            current = selection_time if cls.calls == 1 else delay_time
            return current if tz is not None else current.replace(tzinfo=None)

    scheduled: list[str] = []
    observed_delays: list[float] = []

    async def fake_schedule(_session_factory, source_id: str, **_kwargs) -> bool:
        scheduled.append(source_id)
        return False

    async def fake_sleep(delay: float) -> None:
        observed_delays.append(delay)
        if len(observed_delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("server.enterprise_identity.sync.datetime", FrozenDateTime)
    monkeypatch.setattr(
        "server.enterprise_identity.sync.schedule_identity_source_sync",
        fake_schedule,
    )
    monkeypatch.setattr("server.enterprise_identity.sync.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await identity_source_sync_loop(factory, interval_seconds=1800)

    assert observed_delays == [0.0, 5.0]
    assert scheduled == [source.id]


@pytest.mark.asyncio
async def test_identity_source_sync_loop_retries_when_retry_query_fails(
    monkeypatch,
) -> None:
    observed_delays: list[float] = []

    async def fake_sync(_session_factory, **_kwargs) -> bool:
        return False

    async def failing_next_delay(*_args, **_kwargs) -> float:
        raise RuntimeError("database unavailable")

    async def fake_sleep(delay: float) -> None:
        observed_delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "server.enterprise_identity.sync.sync_configured_identity_sources_once",
        fake_sync,
    )
    monkeypatch.setattr(
        "server.enterprise_identity.sync._next_identity_source_sync_delay",
        failing_next_delay,
    )
    monkeypatch.setattr("server.enterprise_identity.sync.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await identity_source_sync_loop(object(), interval_seconds=1800)

    assert observed_delays == [5.0]


@pytest.mark.asyncio
async def test_configured_sync_interval_controls_active_source_due_time(
    session, monkeypatch
) -> None:
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    source = await _source(session, "tenant-custom-interval")
    source.config_ciphertext = encrypt(
        json.dumps({"app_id": "app-id", "app_secret": "secret"})
    )
    source.status = "active"
    source.last_synced_at = datetime.now(UTC) - timedelta(seconds=90)
    await session.commit()
    calls = 0

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            nonlocal calls
            calls += 1
            return FeishuDirectorySnapshot(users=[], groups=[], memberships=[])

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=1800,
        feishu_client_factory=FakeClient,
    )
    assert calls == 0

    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=60,
        feishu_client_factory=FakeClient,
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_identity_sync_claim_survives_independent_scheduler_state(
    session,
) -> None:
    source = await _source(session, "tenant-shared-lease")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    source.sync_worker_id = "other-process"
    source.sync_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    assert await schedule_identity_source_sync(factory, source.id) is False

    async with factory() as database_session:
        stored = await database_session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        stored.sync_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await database_session.commit()

    calls = 0

    async def fake_sync(_session, _source):
        nonlocal calls
        calls += 1
        _source.status = EnterpriseIdentitySourceStatus.ACTIVE

    assert await schedule_identity_source_sync(
        factory,
        source.id,
        sync_callable=fake_sync,
    ) is True
    task = sync_task(source.id)
    assert task is not None
    await task
    assert calls == 1


@pytest.mark.asyncio
async def test_sharepoint_503_persists_backoff_and_skips_immediate_retry(
    session, monkeypatch
) -> None:
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    source = EnterpriseIdentitySource.create(
        name="SharePoint backoff",
        provider=IdentitySourceProvider.SHAREPOINT,
        tenant_id="tenant-sharepoint-backoff",
    )
    source.config_ciphertext = encrypt(json.dumps({
        "cloud": "global",
        "client_id": "client",
        "client_secret": "secret",
    }))
    session.add(source)
    await session.commit()
    calls = 0

    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def fetch_snapshot(self):
            nonlocal calls
            calls += 1
            request = httpx.Request("GET", "https://graph.microsoft.test/users")
            raise httpx.HTTPStatusError(
                "unavailable",
                request=request,
                response=httpx.Response(503, request=request),
            )

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(
        factory,
        sharepoint_client_factory=FailingClient,
    )
    await sync_configured_identity_sources_once(
        factory,
        sharepoint_client_factory=FailingClient,
    )

    async with factory() as database_session:
        stored = await database_session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        assert stored.status == EnterpriseIdentitySourceStatus.STALE
        assert stored.sync_retry_count == 1
        assert stored.sync_next_retry_at is not None
    assert calls == 1


def test_feishu_sync_defaults_to_two_initial_and_one_incremental_request() -> None:
    assert _feishu_sync_concurrency(initial_sync=True) == 2
    assert _feishu_sync_concurrency(initial_sync=False) == 1


def test_retryable_500_and_503_failures_back_off_consecutively() -> None:
    request = httpx.Request("GET", "https://open.feishu.cn/example")

    first = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=httpx.Response(500, request=request),
    )
    second = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=httpx.Response(503, request=request),
    )

    assert _retry_delay_seconds(first, 1, fallback_seconds=1800) == 60.0
    assert _retry_delay_seconds(second, 2, fallback_seconds=1800) == 120.0


def test_retry_after_and_nonretryable_failures_are_bounded() -> None:
    request = httpx.Request("GET", "https://graph.microsoft.test/example")
    throttled = httpx.HTTPStatusError(
        "throttled",
        request=request,
        response=httpx.Response(429, headers={"Retry-After": "90"}, request=request),
    )
    forbidden = httpx.HTTPStatusError(
        "forbidden",
        request=request,
        response=httpx.Response(403, request=request),
    )

    assert _retry_delay_seconds(throttled, 1, fallback_seconds=1800) == 90.0
    assert _retry_delay_seconds(forbidden, 1, fallback_seconds=1800) == 1800.0
