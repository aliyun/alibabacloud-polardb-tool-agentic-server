from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import base64
import json
import os
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.enterprise_identity.providers import _feishu_sync_concurrency
from server.enterprise_identity.service import (
    _StreamingDirectorySink,
    upsert_directory_group,
    upsert_directory_membership,
    upsert_external_user,
    replace_directory_snapshot,
    sync_identity_source,
    apply_feishu_directory_event,
)
from server.enterprise_identity.sync import (
    _feishu_event_lock,
    _next_identity_source_sync_delay,
    _retry_delay_seconds,
    identity_source_sync_loop,
    request_feishu_full_sync_on_startup,
    schedule_identity_source_sync,
    sync_configured_identity_sources_once,
    sync_task,
)
from server.enterprise_identity import service as identity_service
from server.config import EnterpriseIdentitySyncConfig
from types import SimpleNamespace
from server.models import (
    AuthProvider,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryUser,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    FeishuEventSubscriptionStatus,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
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
    UserRefreshToken,
    UserStatus,
)
from server.models.oauth import ExternalTokenSession, OAuthRefreshToken
from server.polarrag.identity import (
    IdentityContextUnavailable,
    resolve_acl_context,
)
from server.polarrag.access import list_visible_knowledge_resources, plan_knowledge_access
from server.core.crypto import encrypt
from server.enterprise_identity.feishu import FeishuDirectoryClient, FeishuDirectoryError, FeishuDirectorySnapshot
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


class _AsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


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

    class FakeClient(_AsyncClient):
        def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, cloud: str):
            assert (tenant_id, client_id, client_secret, cloud) == (
                "tenant-001", "client-id", "client-secret", "global"
            )

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
        factory, provider_client_factories={IdentitySourceProvider.SHAREPOINT: FakeClient}
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

    class FakeClient(_AsyncClient):
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

    class FakeClient(_AsyncClient):
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
async def test_feishu_streaming_sync_preserves_memberships_for_unavailable_group(session) -> None:
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
            groups = (
                [{"group_id": "g-existing", "name": "Existing"}]
                if request.url.params["type"] == "1"
                else []
            )
            return httpx.Response(200, json={
                "code": 0, "data": {"grouplist": groups, "has_more": False},
            })
        if request.url.path == "/open-apis/contact/v3/group/g-existing/member/simplelist":
            if request.url.params.get("page_token") is None:
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {
                        "memberlist": [{"member_id": "u-ok", "member_type": "user"}],
                        "has_more": True,
                        "page_token": "members-page-2",
                    },
                })
            if fail_membership:
                return httpx.Response(400, json={"code": 99991672}, request=request)
            return httpx.Response(200, json={
                "code": 0, "data": {"memberlist": [], "has_more": False},
            })
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
async def test_feishu_incremental_client_reads_one_user_department_and_memberships() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path == "/open-apis/contact/v3/users/u1":
            return httpx.Response(200, json={"code": 0, "data": {"user": {
                "user_id": "u1", "name": "Alice", "email": "alice@example.test",
                "department_ids": ["0", "d1"],
            }}})
        if request.url.path == "/open-apis/contact/v3/departments/d1":
            return httpx.Response(200, json={"code": 0, "data": {"department": {
                "open_department_id": "d1", "name": "Engineering",
                "parent_department_id": "d0",
            }}})
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            groups = [{"group_id": "g1"}] if request.url.params["group_type"] == "1" else []
            return httpx.Response(200, json={"code": 0, "data": {
                "group_list": groups, "has_more": False,
            }})
        raise AssertionError(request.url)

    async with FeishuDirectoryClient(
        app_id="app",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.fetch_user("u1") == {
            "id": "u1", "display_name": "Alice", "email": "alice@example.test",
        }
        assert await client.fetch_department("d1") == {
            "id": "d1", "display_name": "Engineering",
            "principal_type": "department", "parent_id": "d0",
        }
        assert await client.fetch_user_memberships("u1") == [
            {"group_id": "d1", "member_type": "user", "member_id": "u1"},
            {"group_id": "g1", "member_type": "user", "member_id": "u1"},
        ]


@pytest.mark.asyncio
async def test_feishu_user_memberships_reject_missing_department_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path == "/open-apis/contact/v3/users/u1":
            return httpx.Response(200, json={"code": 0, "data": {"user": {
                "user_id": "u1", "name": "Alice",
            }}})
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            return httpx.Response(200, json={"code": 0, "data": {"group_list": [], "has_more": False}})
        raise AssertionError(request.url)

    async with FeishuDirectoryClient(
        app_id="app", app_secret="secret", transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FeishuDirectoryError, match="department_ids"):
            await client.fetch_user_memberships("u1")


@pytest.mark.asyncio
async def test_feishu_incremental_client_lists_direct_department_users() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path == "/open-apis/contact/v3/users/find_by_department":
            assert request.url.params["department_id"] == "d1"
            assert request.url.params["department_id_type"] == "open_department_id"
            assert request.url.params["user_id_type"] == "user_id"
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"user_id": "u1", "name": "Alice"},
            ], "has_more": False}})
        raise AssertionError(request.url)

    async with FeishuDirectoryClient(
        app_id="app", app_secret="secret", transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.fetch_department_users("d1") == [
            {"id": "u1", "display_name": "Alice", "email": None},
        ]


async def test_feishu_group_snapshot_reads_groups_and_members_without_users_or_departments() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path == "/open-apis/contact/v3/group/simplelist":
            groups = [{"id": "g1", "name": "Engineering"}] if request.url.params["type"] == "1" else []
            return httpx.Response(200, json={"code": 0, "data": {"grouplist": groups, "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/g1/member/simplelist":
            assert request.url.params["member_type"] == "user"
            assert request.url.params["member_id_type"] == "user_id"
            return httpx.Response(200, json={"code": 0, "data": {
                "memberlist": [{"member_id": "u1", "member_type": "user"}], "has_more": False,
            }})
        raise AssertionError(request.url)

    async with FeishuDirectoryClient(
        app_id="app", app_secret="secret", transport=httpx.MockTransport(handler),
    ) as client:
        snapshot = await client.fetch_group_snapshot()

    assert snapshot.groups == [{"id": "g1", "display_name": "Engineering", "principal_type": "group"}]
    assert snapshot.memberships == [{"group_id": "g1", "member_type": "user", "member_id": "u1"}]
    assert set(paths) == {
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/contact/v3/group/simplelist",
        "/open-apis/contact/v3/group/g1/member/simplelist",
    }


async def test_feishu_group_reconciliation_replaces_only_group_facts(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-group-only")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime(2026, 9, 20, tzinfo=UTC)
    for user_id in ("u1", "u2"):
        await upsert_external_user(session, source, external_user_id=user_id, display_name=user_id, email=None)
    for group_id, principal_type in (("g1", EnterpriseDirectoryPrincipalType.GROUP),
                                     ("old", EnterpriseDirectoryPrincipalType.GROUP),
                                     ("d1", EnterpriseDirectoryPrincipalType.DEPARTMENT)):
        await upsert_directory_group(session, source, external_group_id=group_id,
                                     display_name=group_id, principal_type=principal_type)
    for group_id in ("g1", "old", "d1"):
        await upsert_directory_membership(session, source, external_group_id=group_id,
                                          member_type=EnterpriseDirectoryMembershipType.USER,
                                          external_member_id="u1")
    await session.commit()

    class FakeClient(_AsyncClient):
        def __init__(self, **kwargs):
            assert (kwargs["app_id"], kwargs["app_secret"]) == ("app", "secret")

        async def fetch_group_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[],
                groups=[{"id": "g1", "display_name": "Renamed", "principal_type": "group"}],
                memberships=[{"group_id": "g1", "member_type": "user", "member_id": "u2"}],
            )

    await identity_service.reconcile_feishu_groups(session, source, feishu_client_factory=FakeClient)
    await session.commit()
    groups = {row.external_group_id: row for row in (await session.execute(
        select(EnterpriseDirectoryGroup).where(EnterpriseDirectoryGroup.identity_source_id == source.id)
    )).scalars()}
    memberships = {(row.external_group_id, row.external_member_id) for row in (await session.execute(
        select(EnterpriseDirectoryMembership).where(EnterpriseDirectoryMembership.identity_source_id == source.id)
    )).scalars()}
    assert groups["g1"].display_name == "Renamed"
    assert groups["old"].status == EnterpriseDirectoryEntryStatus.DISABLED
    assert groups["d1"].status == EnterpriseDirectoryEntryStatus.ACTIVE
    assert memberships == {("g1", "u2"), ("d1", "u1")}
    assert source.last_synced_at == datetime(2026, 9, 20, tzinfo=UTC)
    assert source.last_group_synced_at is not None


@pytest.mark.asyncio
async def test_group_reconciliation_keeps_etl_chat_and_acl_groups(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-etl-chat")
    source.config_ciphertext = encrypt(json.dumps({
        "app_id": "app", "app_secret": "secret",
        "acl_membership_snapshot": {
            "host": "meta.example.test", "port": 3306,
            "username": "readonly", "password": "snapshot-secret",
        },
    }))
    await replace_directory_snapshot(
        session, source,
        users=[{"id": "u1", "display_name": "User", "email": None}],
        groups=[
            {"id": "oc_chat", "display_name": "Chat", "principal_type": "group"},
            {"id": "oc_removed", "display_name": "Removed chat", "principal_type": "group"},
            {"id": "g_both", "display_name": "Old name", "principal_type": "group"},
            {"id": "acl_group-1", "display_name": "ACL", "principal_type": "acl_group"},
        ],
        memberships=[
            {"group_id": group_id, "member_type": "user", "member_id": "u1"}
            for group_id in ("oc_chat", "oc_removed", "acl_group-1")
        ],
    )
    await session.commit()

    class Client(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_group_snapshot(self):
            return FeishuDirectorySnapshot(
                users=[], groups=[{"id": "g_both", "display_name": "Contact group"}],
                memberships=[],
            )

    class AclClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self, *, tenant_id):
            assert tenant_id == source.tenant_id
            return SimpleNamespace(
                groups=[
                    {"id": "oc_chat", "display_name": "Chat", "principal_type": "group"},
                    {"id": "g_both", "display_name": "g_both", "principal_type": "group"},
                    {"id": "acl_group-1", "display_name": "ACL", "principal_type": "acl_group"},
                ],
                memberships=[
                    {"group_id": group_id, "member_type": "user", "member_id": "u1"}
                    for group_id in ("oc_chat", "acl_group-1")
                ],
            )

    await identity_service.reconcile_feishu_groups(
        session, source, feishu_client_factory=Client,
        acl_snapshot_client_factory=AclClient,
    )
    await session.commit()
    groups = {group.external_group_id: group for group in (
        await session.scalars(select(EnterpriseDirectoryGroup))
    )}
    memberships = set((await session.scalars(select(
        EnterpriseDirectoryMembership.external_group_id
    ))).all())
    assert groups["oc_chat"].status == EnterpriseDirectoryEntryStatus.ACTIVE
    assert groups["oc_removed"].status == EnterpriseDirectoryEntryStatus.DISABLED
    assert groups["g_both"].display_name == "Contact group"
    assert memberships == {"oc_chat", "acl_group-1"}


@pytest.mark.asyncio
async def test_group_reconciliation_keeps_user_event_applied_during_fetch(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-group-event-overlap")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    await upsert_external_user(session, source, external_user_id="u1", display_name="u1", email=None)
    await upsert_directory_group(session, source, external_group_id="g1", display_name="g1")
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_group_snapshot(self):
            async with factory() as event_session:
                event_session.add(EnterpriseIdentitySourceEvent(
                    identity_source_id=source.id,
                    event_type="contact.user.deleted_v3",
                    subject_id="u1",
                    status=EnterpriseIdentitySourceEventStatus.PROCESSED,
                ))
                await event_session.execute(
                    update(EnterpriseDirectoryUser)
                    .where(EnterpriseDirectoryUser.identity_source_id == source.id)
                    .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
                )
                await event_session.commit()
            return FeishuDirectorySnapshot(
                users=[], groups=[{"id": "g1", "display_name": "g1", "principal_type": "group"}],
                memberships=[{"group_id": "g1", "member_type": "user", "member_id": "u1"}],
            )

    await identity_service.reconcile_feishu_groups(session, source, feishu_client_factory=FakeClient)
    memberships = list((await session.execute(select(EnterpriseDirectoryMembership).where(
        EnterpriseDirectoryMembership.identity_source_id == source.id,
    ))).scalars())
    assert memberships == []


@pytest.mark.asyncio
async def test_group_reconciliation_does_not_apply_stale_snapshot_after_full_request(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-group-full-request")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.sync_kind = "groups"
    source.sync_worker_id = "group-worker"
    await upsert_external_user(session, source, external_user_id="u1", display_name="u1", email=None)
    await upsert_directory_group(session, source, external_group_id="g1", display_name="g1")
    await upsert_directory_membership(
        session, source, external_group_id="g1",
        member_type=EnterpriseDirectoryMembershipType.USER, external_member_id="u1",
    )
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_group_snapshot(self):
            async with factory() as other:
                stored = await other.get(EnterpriseIdentitySource, source.id)
                stored.full_sync_requested_at = datetime.now(UTC)
                await other.commit()
            return FeishuDirectorySnapshot(
                users=[], groups=[{"id": "g1", "display_name": "g1", "principal_type": "group"}],
                memberships=[],
            )

    with pytest.raises(RuntimeError, match="Full reconciliation requested"):
        await identity_service.reconcile_feishu_groups(session, source, feishu_client_factory=FakeClient)
    await session.rollback()
    memberships = list((await session.scalars(select(EnterpriseDirectoryMembership))).all())
    assert [(item.external_group_id, item.external_member_id) for item in memberships] == [("g1", "u1")]


async def test_scheduler_runs_due_group_reconciliation_without_full_directory_sync(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    now = datetime.now(UTC)
    source = await _source(session, "tenant-group-schedule")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = now - timedelta(minutes=20)
    source.last_group_synced_at = now - timedelta(hours=2)
    source.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
    await session.commit()

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_group_snapshot(self):
            assert not _feishu_event_lock.locked(), "group fetch must not block event processing"
            return FeishuDirectorySnapshot(users=[], groups=[], memberships=[])

        async def fetch_snapshot(self):
            pytest.fail("group reconciliation must not scan the full directory")

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(factory, feishu_client_factory=FakeClient, now=now)

    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert stored.last_group_synced_at.replace(tzinfo=UTC) > now - timedelta(minutes=1)
        assert stored.last_synced_at.replace(tzinfo=UTC) == source.last_synced_at.replace(tzinfo=UTC)


@pytest.mark.parametrize(("case", "sync_kind", "paused", "request_full"), [
    ("recent", None, False, True),
    ("running-full", "full", False, False),
    ("running-groups", "groups", False, True),
    ("paused", None, True, False),
])
async def test_restart_requests_full_only_when_needed(
    session, case, sync_kind, paused, request_full,
) -> None:
    source = await _source(session, f"tenant-restart-{case}")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    if case == "recent":
        source.last_synced_at = datetime.now(UTC)
    if sync_kind:
        source.sync_worker_id = f"{sync_kind}-worker"
        source.sync_kind = sync_kind
        source.sync_lease_until = datetime.now(UTC) + timedelta(minutes=2)
    if paused:
        source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    await request_feishu_full_sync_on_startup(factory)

    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert (stored.full_sync_requested_at is not None) is request_full


async def test_fallback_full_waits_for_active_group_reconciliation(session) -> None:
    now = datetime.now(UTC)
    source = await _source(session, "tenant-fallback-behind-group")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    source.last_synced_at = now - timedelta(hours=25)
    source.event_unavailable_since = now - timedelta(hours=25)
    source.sync_kind = "groups"
    source.sync_worker_id = "group-worker"
    source.sync_lease_until = now + timedelta(minutes=2)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    await sync_configured_identity_sources_once(factory, now=now)

    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert stored.sync_kind == "groups"
        assert stored.full_sync_requested_at is not None


async def test_failed_group_reconciliation_keeps_source_active_and_schedules_retry(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-group-failure")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC) - timedelta(minutes=20)
    source.last_group_synced_at = datetime.now(UTC) - timedelta(hours=2)
    await session.commit()

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_group_snapshot(self):
            raise RuntimeError("sensitive provider response")

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(factory, feishu_client_factory=FakeClient)
    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert stored.status == EnterpriseIdentitySourceStatus.ACTIVE
        assert stored.last_group_error == "RuntimeError"
        assert stored.group_next_retry_at is not None
        assert stored.sync_next_retry_at is None


async def test_feishu_fallback_uses_configured_unavailability_threshold(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    monkeypatch.setattr("server.enterprise_identity.sync.get_config", lambda: SimpleNamespace(
        enterprise_identity_sync=EnterpriseIdentitySyncConfig(feishu_fallback_after_seconds=3600),
    ))
    source = await _source(session, "tenant-short-fallback")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC) - timedelta(hours=2)
    source.last_group_synced_at = datetime.now(UTC)
    source.event_unavailable_since = datetime.now(UTC) - timedelta(hours=2)
    await session.commit()
    calls = 0

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self):
            nonlocal calls
            calls += 1
            return FeishuDirectorySnapshot(users=[], groups=[], memberships=[])

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(factory, feishu_client_factory=FakeClient)
    assert calls == 1


@pytest.mark.asyncio
async def test_feishu_subscription_error_refreshes_groups_while_waiting_for_full_fallback(session, monkeypatch) -> None:
    source = await _source(session, "tenant-paused-subscription")
    source.config_ciphertext = "configured"
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
    source.last_synced_at = datetime.now(UTC) - timedelta(days=2)
    source.last_group_synced_at = datetime.now(UTC) - timedelta(days=2)
    source.event_unavailable_since = datetime.now(UTC) - timedelta(hours=23)
    await session.commit()

    scheduled = []

    async def schedule_groups(*_args, **kwargs):
        scheduled.append(kwargs)
        return True

    monkeypatch.setattr("server.enterprise_identity.sync.schedule_identity_source_sync", schedule_groups)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(factory)
    assert len(scheduled) == 1
    assert scheduled[0]["sync_callable"].__name__ == "reconcile_feishu_groups"


@pytest.mark.asyncio
async def test_manual_full_sync_connects_before_snapshot_and_discards_old_events(session, monkeypatch) -> None:
    source = await _source(session, "tenant-resume-subscription")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
    session.add(EnterpriseIdentitySourceEvent(
        identity_source_id=source.id,
        event_type="contact.user.updated_v3",
        subject_id="user-1",
        next_retry_at=datetime.now(UTC) + timedelta(hours=1),
    ))
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def preconnect(session_factory, source_id):
        assert not _feishu_event_lock.locked()
        async with session_factory() as link_session:
            stored = await link_session.get(EnterpriseIdentitySource, source_id)
            stored.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
            await link_session.commit()
        return True

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.reconnect_feishu_listener_before_full", preconnect,
    )

    async def successful_sync(_session, stored):
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        stored.last_synced_at = datetime.now(UTC)

    assert await schedule_identity_source_sync(
        factory, source.id, sync_callable=successful_sync,
    )
    await asyncio.wait_for(sync_task(source.id), timeout=2)
    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        event = await verify_session.scalar(select(EnterpriseIdentitySourceEvent).where(
            EnterpriseIdentitySourceEvent.identity_source_id == source.id,
        ))
        assert event.status == EnterpriseIdentitySourceEventStatus.PROCESSED


@pytest.mark.asyncio
async def test_failed_manual_full_sync_keeps_subscription_paused(session, monkeypatch) -> None:
    source = await _source(session, "tenant-still-paused")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def failed_preconnect(_session_factory, _source_id):
        return False

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.reconnect_feishu_listener_before_full", failed_preconnect,
    )

    async def failing_sync(_session, _source):
        raise RuntimeError("sync failed")

    assert await schedule_identity_source_sync(
        factory, source.id, sync_callable=failing_sync,
    )
    await sync_task(source.id)
    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ERROR


@pytest.mark.asyncio
async def test_full_sync_continues_when_preconnection_fails(session, monkeypatch) -> None:
    source = await _source(session, "tenant-full-without-link")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    calls = []

    async def failed_preconnect(_session_factory, _source_id):
        calls.append("connect")
        return False

    async def full_sync(_session, stored):
        calls.append("full")
        stored.last_synced_at = datetime.now(UTC)

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.reconnect_feishu_listener_before_full", failed_preconnect,
    )
    assert await schedule_identity_source_sync(factory, source.id, sync_callable=full_sync)
    await sync_task(source.id)
    assert calls == ["connect", "full"]
    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ERROR
        assert stored.last_synced_at is not None


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

    class FakeClient(_AsyncClient):
        def __init__(self, *, app_id: str, app_secret: str):
            assert (app_id, app_secret) == ("cli_test", "secret")

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

    class FakeClient(_AsyncClient):
        def __init__(self, *, app_id: str, app_secret: str):
            assert (app_id, app_secret) == ("cli_test", "secret")

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

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

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
                "dialect": "mysql",
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

    class FakeClient(_AsyncClient):
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
async def test_identity_source_scheduler_checks_feishu_fallback_without_delaying_events(
    session,
    monkeypatch,
) -> None:
    fixed_now = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    source = await _source(session, "tenant-daily-wakeup")
    source.config_ciphertext = "configured"
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = fixed_now - timedelta(hours=23)
    source.last_group_synced_at = fixed_now
    source.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr("server.enterprise_identity.sync.datetime", FrozenDateTime)

    assert await _next_identity_source_sync_delay(
        factory,
        configured_interval=48 * 60 * 60,
        selected_at=fixed_now,
    ) == 600

    source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
    source.event_unavailable_since = fixed_now - timedelta(hours=23)
    await session.commit()
    assert await _next_identity_source_sync_delay(
        factory,
        configured_interval=48 * 60 * 60,
        selected_at=fixed_now,
    ) == 600


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
async def test_feishu_full_reconciliation_only_after_incremental_unavailable_for_day(
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
    source.last_synced_at = datetime.now(UTC) - timedelta(days=2)
    source.last_group_synced_at = datetime.now(UTC)
    source.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
    await session.commit()
    calls = 0

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self):
            nonlocal calls
            calls += 1
            return FeishuDirectorySnapshot(users=[], groups=[], memberships=[])

    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def preconnect(session_factory, source_id):
        async with session_factory() as link_session:
            stored = await link_session.get(EnterpriseIdentitySource, source_id)
            stored.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
            stored.event_unavailable_since = None
            await link_session.commit()
        return True

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.reconnect_feishu_listener_before_full", preconnect,
    )
    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=60,
        feishu_client_factory=FakeClient,
    )
    assert calls == 0

    async with factory() as database_session:
        stored = await database_session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        stored.event_listener_status = FeishuEventSubscriptionStatus.ERROR
        stored.event_unavailable_since = datetime.now(UTC) - timedelta(hours=23)
        await database_session.commit()

    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=60,
        feishu_client_factory=FakeClient,
    )
    assert calls == 0

    async with factory() as database_session:
        stored = await database_session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        stored.event_unavailable_since = datetime.now(UTC) - timedelta(days=1, seconds=1)
        await database_session.commit()

    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=60,
        feishu_client_factory=FakeClient,
    )
    assert calls == 1
    async with factory() as database_session:
        stored = await database_session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        assert stored.event_unavailable_since is None


@pytest.mark.asyncio
async def test_disabled_feishu_source_is_not_selected_for_initial_full_sync(
    session, monkeypatch
) -> None:
    source = await _source(session, "tenant-disabled-full")
    source.config_ciphertext = "configured"
    source.status = EnterpriseIdentitySourceStatus.DISABLED
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def unexpected_schedule(*_args, **_kwargs):
        pytest.fail("disabled source must not be scheduled")

    monkeypatch.setattr(
        "server.enterprise_identity.sync.schedule_identity_source_sync",
        unexpected_schedule,
    )
    assert await sync_configured_identity_sources_once(factory) is False


@pytest.mark.asyncio
async def test_sharepoint_keeps_configured_full_sync_interval(session, monkeypatch) -> None:
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    source = EnterpriseIdentitySource.create(
        name="SharePoint configured interval",
        provider=IdentitySourceProvider.SHAREPOINT,
        tenant_id="tenant-sharepoint-interval",
    )
    source.config_ciphertext = encrypt(json.dumps({
        "cloud": "global",
        "client_id": "client",
        "client_secret": "secret",
    }))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC) - timedelta(seconds=90)
    session.add(source)
    await session.commit()
    calls = 0

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self):
            nonlocal calls
            calls += 1
            return SharePointDirectorySnapshot(users=[], groups=[], memberships=[])

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=1800,
        sharepoint_client_factory=FakeClient,
    )
    assert calls == 0

    await sync_configured_identity_sources_once(
        factory,
        interval_seconds=60,
        sharepoint_client_factory=FakeClient,
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_sharepoint_full_sync_request_bypasses_interval(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = EnterpriseIdentitySource.create(
        name="SharePoint requested sync",
        provider=IdentitySourceProvider.SHAREPOINT,
        tenant_id="tenant-sharepoint-request",
    )
    source.config_ciphertext = encrypt(json.dumps({
        "cloud": "global", "client_id": "client", "client_secret": "secret",
    }))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC)
    source.full_sync_requested_at = datetime.now(UTC)
    session.add(source)
    await session.commit()

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self):
            return SharePointDirectorySnapshot(users=[], groups=[], memberships=[])

    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(
        factory, interval_seconds=1800, sharepoint_client_factory=FakeClient,
    )
    await session.refresh(source)
    assert source.full_sync_requested_at is None
    assert source.last_synced_at is not None


@pytest.mark.asyncio
async def test_scheduler_uses_registered_provider_sync_policy(session, monkeypatch) -> None:
    from server.enterprise_identity.providers import (
        DIRECTORY_SYNC_ADAPTERS,
        SharePointDirectorySyncAdapter,
    )

    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = EnterpriseIdentitySource.create(
        name="SharePoint adapter policy",
        provider=IdentitySourceProvider.SHAREPOINT,
        tenant_id="tenant-adapter-policy",
    )
    source.config_ciphertext = encrypt(json.dumps({
        "cloud": "global", "client_id": "client", "client_secret": "secret",
    }))
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC)
    session.add(source)
    await session.commit()

    class ImmediateAdapter(SharePointDirectorySyncAdapter):
        def due_sync(self, _source, _now, _settings, _interval):
            return SimpleNamespace(kind="full", interval_seconds=60)

    class FakeClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

        async def fetch_snapshot(self):
            return SharePointDirectorySnapshot(
                users=[{"id": "u1", "display_name": "Alice", "email": None}],
                groups=[], memberships=[],
            )

    monkeypatch.setitem(DIRECTORY_SYNC_ADAPTERS, IdentitySourceProvider.SHAREPOINT, ImmediateAdapter())
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    await sync_configured_identity_sources_once(
        factory,
        provider_client_factories={IdentitySourceProvider.SHAREPOINT: FakeClient},
    )

    async with factory() as verify_session:
        user = await verify_session.scalar(select(EnterpriseDirectoryUser).where(
            EnterpriseDirectoryUser.identity_source_id == source.id,
        ))
        assert user is not None and user.external_user_id == "u1"


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
async def test_feishu_full_sync_waits_for_received_incremental_events(
    session, monkeypatch
) -> None:
    source = await _source(session, "tenant-drain-before-full")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    event = EnterpriseIdentitySourceEvent(
        identity_source_id=source.id,
        event_type="contact.user.updated_v3",
        subject_id="user-1",
    )
    session.add(event)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    order: list[str] = []

    async def fake_process(_factory, *, source_ids, draining_worker_id, event_cutoff):
        assert source_ids == {source.id}
        assert draining_worker_id
        assert event_cutoff
        order.append("incremental")
        async with _factory() as event_session:
            stored = await event_session.get(EnterpriseIdentitySourceEvent, event.id)
            assert stored is not None
            stored.status = EnterpriseIdentitySourceEventStatus.PROCESSED
            await event_session.commit()
        return 1

    async def fake_full(_session, _source):
        order.append("full")

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.process_feishu_directory_events_once",
        fake_process,
    )
    assert await schedule_identity_source_sync(
        factory, source.id, sync_callable=fake_full
    ) is True
    task = sync_task(source.id)
    assert task is not None
    await task
    assert order == ["incremental", "full"]


@pytest.mark.asyncio
async def test_feishu_full_sync_drains_real_pending_event_before_snapshot(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/events.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        source = await _source(database_session, "tenant-real-drain")
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.config_ciphertext = "configured"
        source_id = source.id
        database_session.add(EnterpriseIdentitySourceEvent(
            identity_source_id=source_id,
            event_type="contact.user.deleted_v3",
            subject_id="user-1",
        ))
        await database_session.commit()

    async def full(_session, _source):
        async with factory() as event_session:
            event = await event_session.scalar(select(EnterpriseIdentitySourceEvent))
            assert event.status == EnterpriseIdentitySourceEventStatus.PROCESSED

    try:
        assert await schedule_identity_source_sync(factory, source_id, sync_callable=full)
        task = sync_task(source_id)
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
        async with factory() as database_session:
            source = await database_session.get(EnterpriseIdentitySource, source_id)
            assert source.sync_kind is None
    finally:
        task = sync_task(source_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_manual_feishu_full_sync_retries_after_backoff(session) -> None:
    from server.enterprise_identity.providers import FeishuDirectorySyncAdapter

    source = await _source(session, "tenant-full-retry")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    source.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC)
    source.last_group_synced_at = datetime.now(UTC) - timedelta(hours=2)
    source.full_sync_requested_at = datetime.now(UTC)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def failed_full(_session, _source):
        raise RuntimeError("temporary upstream failure")

    assert await schedule_identity_source_sync(
        factory, source.id, sync_callable=failed_full, interval_seconds=60
    )
    task = sync_task(source.id)
    assert task is not None
    await task

    assert await _next_identity_source_sync_delay(factory, 1800) > 1

    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySource, source.id)
        retry_at = stored.sync_next_retry_at.replace(tzinfo=UTC)
        adapter = FeishuDirectorySyncAdapter()
        due = adapter.due_sync(
            stored, retry_at + timedelta(seconds=1), EnterpriseIdentitySyncConfig(), 1800
        )
        assert due is not None and due.kind == "full"


@pytest.mark.asyncio
async def test_feishu_events_received_during_full_sync_apply_after_completion(session) -> None:
    from server.enterprise_identity.feishu_events import process_feishu_directory_events_once

    source = await _source(session, "tenant-event-during-full")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    order: list[str] = []

    async def fake_full(sync_session, sync_source):
        assert not _feishu_event_lock.locked()
        assert sync_source.sync_kind == "full"
        order.append("full")
        sync_session.add(EnterpriseIdentitySourceEvent(
            identity_source_id=sync_source.id,
            event_type="contact.user.deleted_v3",
            subject_id="user-1",
        ))

    assert await schedule_identity_source_sync(factory, source.id, sync_callable=fake_full)
    task = sync_task(source.id)
    assert task is not None
    await task

    async def fake_apply(_session, _source, **_kwargs):
        order.append("event")
        return False

    assert await process_feishu_directory_events_once(factory, apply_callable=fake_apply) == 1
    assert order == ["full", "event"]
    async with factory() as verify_session:
        stored = await verify_session.scalar(select(EnterpriseIdentitySourceEvent).where(
            EnterpriseIdentitySourceEvent.subject_id == "user-1",
        ))
        assert stored.status == EnterpriseIdentitySourceEventStatus.PROCESSED


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [True, False], ids=["success", "failure"])
async def test_full_sync_only_supersedes_failed_events_on_success(session, succeeds) -> None:
    from server.enterprise_identity.feishu_events import prune_expired_identity_source_events

    source = await _source(session, f"tenant-full-failed-event-{succeeds}")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.config_ciphertext = "configured"
    event = EnterpriseIdentitySourceEvent(
        identity_source_id=source.id,
        event_type="contact.user.updated_v3",
        subject_id="user-1",
        status=EnterpriseIdentitySourceEventStatus.FAILED,
        created_at=datetime.now(UTC) - timedelta(days=8),
    )
    session.add(event)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def full_sync(_session, _source):
        if not succeeds:
            raise RuntimeError("provider unavailable")
        _source.last_synced_at = datetime.now(UTC)

    assert await schedule_identity_source_sync(factory, source.id, sync_callable=full_sync)
    task = sync_task(source.id)
    assert task is not None
    await task

    async with factory() as verify_session:
        stored = await verify_session.get(EnterpriseIdentitySourceEvent, event.id)
        assert stored is not None
        assert stored.status == (
            EnterpriseIdentitySourceEventStatus.PROCESSED
            if succeeds else EnterpriseIdentitySourceEventStatus.FAILED
        )
    assert await prune_expired_identity_source_events(factory) == (1 if succeeds else 0)
    async with factory() as verify_session:
        assert (await verify_session.get(EnterpriseIdentitySourceEvent, event.id) is None) is succeeds


@pytest.mark.asyncio
async def test_feishu_event_drain_failure_releases_full_sync_lease(
    session, monkeypatch
) -> None:
    source = await _source(session, "tenant-drain-failure")
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    session.add(EnterpriseIdentitySourceEvent(
        identity_source_id=source.id,
        event_type="contact.user.updated_v3",
        subject_id="user-1",
    ))
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def failing_process(*_args, **_kwargs):
        raise RuntimeError("provider secret must stay private")

    async def unexpected_full(*_args, **_kwargs):
        pytest.fail("full sync must not start before incremental processing")

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.process_feishu_directory_events_once",
        failing_process,
    )
    assert await schedule_identity_source_sync(
        factory, source.id, sync_callable=unexpected_full
    ) is True
    task = sync_task(source.id)
    assert task is not None
    await task
    async with factory() as database_session:
        stored = await database_session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        assert stored.status == EnterpriseIdentitySourceStatus.STALE
        assert stored.last_error == "RuntimeError"
        assert stored.sync_worker_id is None


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

    class FailingClient(_AsyncClient):
        def __init__(self, **_kwargs):
            pass

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


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [
    "contact.department.created_v3", "contact.department.updated_v3",
])
async def test_feishu_department_event_reconciles_direct_users(
    session, monkeypatch, event_type
) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-department-event")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    await upsert_external_user(session, source, external_user_id="old", display_name="Old", email=None)
    await upsert_directory_group(
        session, source, external_group_id="d1", display_name="Department",
        principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
    )
    await upsert_directory_membership(
        session, source, external_group_id="d1",
        member_type=EnterpriseDirectoryMembershipType.USER, external_member_id="old",
    )

    class FakeClient(_AsyncClient):
        async def fetch_department(self, department_id):
            assert department_id == "d1"
            return {"id": "d1", "display_name": "Department", "parent_id": None}

        async def fetch_department_users(self, department_id):
            assert department_id == "d1"
            return [{"id": "u1", "display_name": "Alice", "email": None}]

    await apply_feishu_directory_event(
        session, source, event_type=event_type, subject_id="d1",
        client_factory=lambda **_kwargs: FakeClient(),
    )
    await session.commit()

    user = await session.scalar(select(EnterpriseDirectoryUser).where(
        EnterpriseDirectoryUser.external_user_id == "u1",
    ))
    memberships = list((await session.execute(select(
        EnterpriseDirectoryMembership.external_group_id,
        EnterpriseDirectoryMembership.external_member_id,
    ).where(EnterpriseDirectoryMembership.identity_source_id == source.id))).all())
    assert user is not None and user.status == EnterpriseDirectoryEntryStatus.ACTIVE
    assert memberships == [("d1", "u1")]


@pytest.mark.asyncio
async def test_feishu_user_event_replaces_only_target_user_memberships(session, monkeypatch) -> None:
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    source = await _source(session, "tenant-user-event")
    source.config_ciphertext = encrypt(json.dumps({"app_id": "app", "app_secret": "secret"}))
    await upsert_external_user(
        session, source, external_user_id="u1", display_name="Old", email=None
    )
    await upsert_external_user(
        session, source, external_user_id="u2", display_name="Other", email=None
    )
    for group_id in ("d1", "d2"):
        await upsert_directory_group(
            session,
            source,
            external_group_id=group_id,
            display_name=group_id,
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
        )
    await upsert_directory_membership(
        session,
        source,
        external_group_id="d1",
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="u1",
    )
    await upsert_directory_membership(
        session,
        source,
        external_group_id="d1",
        member_type=EnterpriseDirectoryMembershipType.USER,
        external_member_id="u2",
    )

    class FakeClient(_AsyncClient):
        async def fetch_user(self, user_id):
            assert user_id == "u1"
            return {"id": "u1", "display_name": "Renamed", "email": "u1@example.test"}

        async def fetch_user_memberships(self, user_id):
            assert user_id == "u1"
            return [{"group_id": "d2", "member_type": "user", "member_id": "u1"}]

    needs_full_sync = await apply_feishu_directory_event(
        session,
        source,
        event_type="contact.user.updated_v3",
        subject_id="u1",
        client_factory=lambda **_kwargs: FakeClient(),
    )
    await session.commit()

    assert needs_full_sync is False
    memberships = list((await session.execute(
        select(EnterpriseDirectoryMembership).order_by(
            EnterpriseDirectoryMembership.external_member_id,
            EnterpriseDirectoryMembership.external_group_id,
        )
    )).scalars())
    assert [(row.external_group_id, row.external_member_id) for row in memberships] == [
        ("d2", "u1"),
        ("d1", "u2"),
    ]


@pytest.mark.asyncio
async def test_feishu_user_event_preserves_current_etl_acl_memberships(session, monkeypatch) -> None:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(session, "tenant-user-etl")
    source.config_ciphertext = encrypt(json.dumps({
        "app_id": "app", "app_secret": "secret",
        "acl_membership_snapshot": {
            "host": "meta.example.test", "port": 3306,
            "username": "readonly", "password": "snapshot-secret",
        },
    }))
    await replace_directory_snapshot(
        session, source,
        users=[{"id": "u1", "display_name": "User", "email": None}],
        groups=[
            {"id": "oc_chat", "display_name": "Chat", "principal_type": "group"},
            {"id": "acl_group-1", "display_name": "ACL", "principal_type": "acl_group"},
            {"id": "acl_old", "display_name": "Old ACL", "principal_type": "acl_group"},
        ],
        memberships=[
            {"group_id": group_id, "member_type": "user", "member_id": "u1"}
            for group_id in ("oc_chat", "acl_group-1", "acl_old")
        ],
    )

    class Client(_AsyncClient):
        async def fetch_user(self, user_id):
            return {"id": user_id, "display_name": "Renamed", "email": None}

        async def fetch_user_memberships(self, _user_id):
            return []

    class AclClient:
        def __init__(self, **_kwargs):
            pass

        async def fetch_user_snapshot(self, *, tenant_id, user_id):
            assert (tenant_id, user_id) == (source.tenant_id, "u1")
            return SimpleNamespace(
                groups=[
                    {"id": "oc_chat", "display_name": "Chat", "principal_type": "group"},
                    {"id": "acl_group-1", "display_name": "ACL", "principal_type": "acl_group"},
                ],
                memberships=[
                    {"group_id": group_id, "member_type": "user", "member_id": "u1"}
                    for group_id in ("oc_chat", "acl_group-1")
                ],
            )

    await apply_feishu_directory_event(
        session, source, event_type="contact.user.updated_v3", subject_id="u1",
        client_factory=lambda **_kwargs: Client(),
        acl_snapshot_client_factory=AclClient,
    )
    await session.commit()
    memberships = set((await session.scalars(select(
        EnterpriseDirectoryMembership.external_group_id
    ))).all())
    assert memberships == {"oc_chat", "acl_group-1"}


@pytest.mark.asyncio
async def test_feishu_delete_events_disable_directory_entries_without_deleting_pas_user(session) -> None:
    source = await _source(session, "tenant-delete-event")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="User", email=None
    )
    refresh_token = UserRefreshToken(
        user_id=user.id, token_hash="deleted-user-token", token_family="deleted-user-family",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(refresh_token)
    group = await upsert_directory_group(
        session,
        source,
        external_group_id="d1",
        display_name="Department",
        principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
    )

    await apply_feishu_directory_event(
        session,
        source,
        event_type="contact.user.deleted_v3",
        subject_id="u1",
    )
    await apply_feishu_directory_event(
        session,
        source,
        event_type="contact.department.deleted_v3",
        subject_id="d1",
    )
    await session.flush()

    assert (await session.get(User, user.id)).status == UserStatus.DISABLED
    await session.refresh(refresh_token)
    assert refresh_token.revoked_at is not None
    directory_user = await session.scalar(select(EnterpriseDirectoryUser))
    assert directory_user is not None
    assert directory_user.status == EnterpriseDirectoryEntryStatus.DISABLED
    assert group.status == EnterpriseDirectoryEntryStatus.DISABLED


@pytest.mark.asyncio
async def test_feishu_rejoined_user_reactivates_source_owned_pas_account(session) -> None:
    source = await _source(session, "tenant-rejoin")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Former User", email=None,
    )
    oauth_refresh = OAuthRefreshToken(
        token_hash="rejoined-user-oauth-refresh", client_id="client", user_id=user.id,
        token_family="rejoined-user-family", scopes='["mcp"]',
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    external_session = ExternalTokenSession(
        token_family="rejoined-user-family", user_id=user.id, provider_type="feishu",
        provider_key=f"feishu:{source.tenant_id}", provider_fingerprint="test-fingerprint",
        external_subject="u1", subject_token_ciphertext="test-ciphertext",
        last_validated_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add_all([oauth_refresh, external_session])
    await session.flush()
    await apply_feishu_directory_event(
        session, source, event_type="contact.user.deleted_v3", subject_id="u1",
    )
    await session.refresh(oauth_refresh)
    await session.refresh(external_session)
    assert user.status == UserStatus.DISABLED
    assert oauth_refresh.revoked_at is not None
    assert external_session.revoked_at is not None
    directory_user = await session.scalar(select(EnterpriseDirectoryUser))
    assert directory_user is not None
    deleted_at = directory_user.updated_at

    jitted = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Returned User", email=None,
    )
    assert jitted.id == user.id
    assert user.status == UserStatus.DISABLED
    assert directory_user.status == EnterpriseDirectoryEntryStatus.DISABLED
    assert directory_user.display_name == "Former User"
    assert directory_user.updated_at == deleted_at

    restored = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Returned User", email=None,
        synchronize_status=True,
    )

    assert restored.id == user.id
    assert user.status == UserStatus.ACTIVE
    assert oauth_refresh.revoked_at is not None
    assert external_session.revoked_at is not None
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.ACTIVE


@pytest.mark.asyncio
async def test_feishu_full_sync_reactivates_returned_source_owned_pas_account(session) -> None:
    source = await _source(session, "tenant-full-rejoin")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Former User", email=None,
    )
    await replace_directory_snapshot(session, source, users=[], groups=[], memberships=[])
    assert user.status == UserStatus.DISABLED

    await replace_directory_snapshot(
        session, source,
        users=[{"id": "u1", "display_name": "Returned User"}], groups=[], memberships=[],
    )

    assert user.status == UserStatus.ACTIVE
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.ACTIVE


@pytest.mark.asyncio
async def test_feishu_sync_restores_source_owned_account_when_directory_remains_active(session) -> None:
    source = await _source(session, "tenant-manual-disable")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Active User", email=None,
    )
    user.status = UserStatus.DISABLED
    await session.flush()

    await upsert_external_user(
        session, source, external_user_id="u1", display_name="Active User", email=None,
        synchronize_status=True,
    )

    assert user.status == UserStatus.ACTIVE


@pytest.mark.asyncio
async def test_feishu_delete_event_preserves_pas_managed_user(session) -> None:
    source = await _source(session, "tenant-linked-delete")
    user = User(
        external_id="local-user", display_name="Local User", auth_provider=AuthProvider.BUILTIN,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    await upsert_external_user(
        session, source, external_user_id="u1", display_name="Local User", email=None, user=user,
    )

    await apply_feishu_directory_event(
        session, source, event_type="contact.user.deleted_v3", subject_id="u1",
    )

    assert user.status == UserStatus.ACTIVE
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.DISABLED

    user.status = UserStatus.DISABLED
    await upsert_external_user(
        session, source, external_user_id="u1", display_name="Local User", email=None,
        synchronize_status=True,
    )
    assert user.status == UserStatus.DISABLED
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.ACTIVE


@pytest.mark.asyncio
async def test_feishu_delete_event_preserves_user_with_another_identity(session) -> None:
    source = await _source(session, "tenant-multiple-identities")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Multiple Identities", email=None,
    )
    session.add(UserExternalIdentity(
        user_id=user.id, identity_provider="other-provider", external_subject="other-user",
    ))
    await session.flush()

    await apply_feishu_directory_event(
        session, source, event_type="contact.user.deleted_v3", subject_id="u1",
    )

    assert user.status == UserStatus.ACTIVE


@pytest.mark.asyncio
async def test_full_sync_disables_missing_source_owned_pas_user(session) -> None:
    source = await _source(session, "tenant-missing-full")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Former User", email=None,
    )

    sink = _StreamingDirectorySink(session, source)
    await sink.begin_sync((datetime.now(UTC) + timedelta(seconds=2)).isoformat())
    await sink.complete_sync()

    assert user.status == UserStatus.DISABLED
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.DISABLED


@pytest.mark.asyncio
async def test_snapshot_sync_disables_missing_source_owned_pas_user(session) -> None:
    source = await _source(session, "tenant-missing-snapshot")
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Former User", email=None,
    )

    await replace_directory_snapshot(session, source, users=[], groups=[], memberships=[])

    assert user.status == UserStatus.DISABLED


@pytest.mark.asyncio
async def test_sharepoint_snapshot_tracks_source_owned_account_status_both_ways(session) -> None:
    source = EnterpriseIdentitySource.create(
        name="SharePoint rehire", provider=IdentitySourceProvider.SHAREPOINT,
        tenant_id="tenant-sharepoint-rehire",
    )
    session.add(source)
    await session.flush()
    user = await upsert_external_user(
        session, source, external_user_id="u1", display_name="Employee", email=None,
    )

    await replace_directory_snapshot(session, source, users=[], groups=[], memberships=[])
    assert user.status == UserStatus.DISABLED
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.DISABLED

    await replace_directory_snapshot(
        session, source,
        users=[{"id": "u1", "display_name": "Employee"}], groups=[], memberships=[],
    )
    assert user.status == UserStatus.ACTIVE
    assert (await session.scalar(select(EnterpriseDirectoryUser))).status == EnterpriseDirectoryEntryStatus.ACTIVE


@pytest.mark.asyncio
async def test_feishu_scope_event_requests_full_reconciliation(session) -> None:
    source = await _source(session, "tenant-scope-event")
    assert await apply_feishu_directory_event(
        session,
        source,
        event_type="contact.scope.updated_v3",
        subject_id=None,
        client_factory=lambda **_kwargs: None,
    ) is True


async def test_feishu_event_fetch_rejects_other_provider_before_credentials(monkeypatch):
    source = EnterpriseIdentitySource.create(
        name="SharePoint", provider=IdentitySourceProvider.SHAREPOINT, tenant_id="tenant-sp",
    )
    source.config_ciphertext = "synthetic ciphertext"
    decrypt = Mock(side_effect=AssertionError("must not decrypt another provider's credentials"))
    client = Mock(side_effect=AssertionError("must not construct a Feishu client"))
    monkeypatch.setattr("server.core.crypto.decrypt", decrypt)
    with pytest.raises(ValueError, match="Feishu directory event source is invalid"):
        await identity_service.fetch_feishu_directory_event(
            source, event_type="contact.user.updated_v3", subject_id="user-1", client_factory=client,
        )
    decrypt.assert_not_called()
    client.assert_not_called()
