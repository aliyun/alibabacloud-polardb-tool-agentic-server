from __future__ import annotations

from datetime import UTC, datetime, timedelta
import base64
import json
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.enterprise_identity.service import (
    upsert_directory_group,
    upsert_directory_membership,
    upsert_external_user,
    replace_directory_snapshot,
    sync_identity_source,
)
from server.enterprise_identity.sync import sync_configured_identity_sources_once
from server.models import (
    AuthProvider,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryUser,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
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
from server.enterprise_identity.feishu import FeishuDirectorySnapshot
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
async def test_stale_source_fails_closed(session):
    source = await _source(session)
    source.status = "active"
    source.last_synced_at = datetime.now(UTC) - timedelta(seconds=source.stale_after_seconds + 1)
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

    with pytest.raises(IdentityContextUnavailable):
        await resolve_acl_context(session, user.id, space.knowledge_space_id)


@pytest.mark.asyncio
async def test_feishu_department_acl_principal_keeps_its_type(session):
    source = await _source(session)
    source.status = "active"
    user = await upsert_external_user(
        session, source, external_user_id="ou_alice", display_name="Alice", email=None
    )
    department = await upsert_directory_group(
        session,
        source,
        external_group_id="od_research",
        display_name="Research",
        principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id=department.external_group_id,
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="ou_alice",
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
