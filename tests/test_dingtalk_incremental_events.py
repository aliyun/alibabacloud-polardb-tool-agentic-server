from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
import base64
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.enterprise_identity.dingtalk import DingTalkDirectoryClient, DingTalkIdentityError
from server.enterprise_identity.dingtalk_auth import verified_dingtalk_config, dingtalk_config_digest
from server.enterprise_identity.dingtalk_incremental import apply_dingtalk_directory_event
from server.enterprise_identity.dingtalk_events import (
    ingest_dingtalk_event,
    normalize_dingtalk_subjects,
    process_dingtalk_directory_events_once,
)
from server.models import (
    EnterpriseDirectoryUser,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    EnterpriseDirectoryEntryStatus,
    IdentitySourceProvider,
    FeishuEventSubscriptionStatus,
)
from tests.test_dingtalk_scope_security import seeded as source_fixture, ding_source
from tests.test_dingtalk_identity import responder

seeded = source_fixture


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"t" * 32).decode())


async def prepare(seeded):
    session, source, *_ = await ding_source(seeded)
    source.event_listener_worker_id = "worker"
    source.event_listener_lease_until = datetime.now(UTC) + timedelta(minutes=10)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    digest = dingtalk_config_digest(source, verified_dingtalk_config(source))

    async def ingest(event_id, event_type="user_modify_org", data=None):
        return await ingest_dingtalk_event(
            factory,
            source.id,
            "worker",
            digest,
            event_id=event_id,
            event_type=event_type,
            corp_id=source.tenant_id,
            data=data if data is not None else {"userId": ["ou-member"]},
        )

    return factory, session, source, ingest


@pytest.mark.parametrize(
    "event_type,data,subjects",
    [
        ("user_modify_org", {"userId": ["u2", "u1", "u1"]}, ["u1", "u2"]),
        ("user_active_org", {"userid": "u1"}, ["u1"]),
        ("org_dept_modify", {"data": {"deptId": [2, 1]}}, ["1", "2"]),
        ("user_leave_org", {"userId": [123]}, [None]),
        ("org_dept_remove", {"deptId": [False]}, [None]),
        ("user_modify_org", {"diffInfo": {"userid": "do-not-infer"}}, [None]),
    ],
)
def test_normalize_batch_and_fail_closed(event_type, data, subjects):
    assert normalize_dingtalk_subjects(event_type, data) == subjects


async def test_inbox_dedup_suspends_only_affected_user_before_ack(seeded):
    factory, session, source, ingest = await prepare(seeded)
    marker = source.last_synced_at
    other = EnterpriseDirectoryUser(identity_source_id=source.id, external_user_id="unrelated", display_name="Other")
    session.add(other)
    await session.commit()
    assert await ingest("batch", data={"userId": ["ou-member", "new-user"]})
    assert not await ingest("batch", data={"userId": ["ou-member", "new-user"]})
    async with factory() as read:
        rows = list(await read.scalars(select(EnterpriseIdentitySourceEvent)))
        assert len(rows) == 2 and {r.subject_id for r in rows} == {"ou-member", "new-user"}
        row = await read.scalar(
            select(EnterpriseDirectoryUser).where(EnterpriseDirectoryUser.external_user_id == "ou-member")
        )
        assert row.status == EnterpriseDirectoryEntryStatus.DISABLED
        row = await read.get(EnterpriseDirectoryUser, other.id)
        assert row.status == EnterpriseDirectoryEntryStatus.ACTIVE
        current = await read.get(EnterpriseIdentitySource, source.id)
        assert current.last_synced_at.replace(tzinfo=UTC) == marker
        assert current.full_sync_requested_at is None


async def test_user_incremental_replaces_memberships_without_advancing_full_marker(seeded):
    factory, session, source, ingest = await prepare(seeded)
    marker = source.last_synced_at
    await ingest("move")
    facts = {
        "user": {"id": "ou-member", "display_name": "Renamed"},
        "groups": [{"id": "3", "display_name": "New department", "principal_type": "department", "parent_id": None}],
        "memberships": [{"group_id": "3", "member_id": "ou-member", "member_type": "user"}],
    }
    assert await process_dingtalk_directory_events_once(factory, fetch_callable=AsyncMock(return_value=facts)) == 1
    async with factory() as read:
        row = await read.scalar(
            select(EnterpriseDirectoryUser).where(EnterpriseDirectoryUser.external_user_id == "ou-member")
        )
        assert row.status == EnterpriseDirectoryEntryStatus.ACTIVE and row.display_name == "Renamed"
        memberships = list(await read.scalars(select(EnterpriseDirectoryMembership)))
        assert [(m.external_group_id, m.external_member_id) for m in memberships] == [("3", "ou-member")]
        current = await read.get(EnterpriseIdentitySource, source.id)
        assert current.last_synced_at.replace(tzinfo=UTC) == marker
        event = await read.scalar(select(EnterpriseIdentitySourceEvent))
        assert event.status == EnterpriseIdentitySourceEventStatus.PROCESSED


async def test_retry_limit_requests_full_and_does_not_mark_transport_broken(seeded):
    factory, _, source, ingest = await prepare(seeded)
    await ingest("retry")
    async with factory() as session:
        row = await session.get(EnterpriseIdentitySource, source.id)
        row.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
        await session.commit()
    fetch = AsyncMock(side_effect=DingTalkIdentityError("TRANSIENT_UPSTREAM"))
    now = datetime.now(UTC)
    for attempt in range(5):
        assert (
            await process_dingtalk_directory_events_once(
                factory, fetch_callable=fetch, now=now + timedelta(hours=attempt)
            )
            == 1
        )
    async with factory() as session:
        row = await session.get(EnterpriseIdentitySource, source.id)
        event = await session.scalar(select(EnterpriseIdentitySourceEvent))
        assert event.status == EnterpriseIdentitySourceEventStatus.FAILED and event.retry_count == 5
        assert row.full_sync_requested_at is not None
        assert row.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        user = await session.scalar(select(EnterpriseDirectoryUser))
        assert user.status == EnterpriseDirectoryEntryStatus.DISABLED


async def test_provider_fence_does_not_consume_feishu_events(seeded):
    factory, session, source, _ = await prepare(seeded)
    source.provider = IdentitySourceProvider.FEISHU
    session.add(
        EnterpriseIdentitySourceEvent(
            identity_source_id=source.id, event_type="contact.user.updated_v3", subject_id="ou-member"
        )
    )
    await session.commit()
    fetch = AsyncMock()
    assert await process_dingtalk_directory_events_once(factory, fetch_callable=fetch) == 0
    fetch.assert_not_called()


async def test_new_notification_during_io_cannot_restore_old_facts(seeded):
    factory, _, _, ingest = await prepare(seeded)
    await ingest("first")

    async def fetch(*args, **kwargs):
        await ingest("newer")
        return {"user": {"id": "ou-member", "display_name": "Stale"}, "groups": [], "memberships": []}

    assert await process_dingtalk_directory_events_once(factory, fetch_callable=fetch) == 0
    async with factory() as session:
        user = await session.scalar(select(EnterpriseDirectoryUser))
        assert user.status == EnterpriseDirectoryEntryStatus.DISABLED


async def test_config_change_during_io_does_not_apply_facts(seeded):
    factory, _, source, ingest = await prepare(seeded)
    await ingest("config")

    async def fetch(*args, **kwargs):
        async with factory() as session:
            row = await session.get(EnterpriseIdentitySource, source.id)
            row.status = "disabled"
            await session.commit()
        return {"user": {"id": "ou-member", "display_name": "Stale"}, "groups": [], "memberships": []}

    assert await process_dingtalk_directory_events_once(factory, fetch_callable=fetch) == 0


async def test_department_delete_requests_full_compensation(seeded):
    factory, _, source, ingest = await prepare(seeded)
    await ingest("department", "org_dept_remove", {"deptId": [2]})
    schedule = AsyncMock()
    assert (
        await process_dingtalk_directory_events_once(
            factory, fetch_callable=AsyncMock(return_value={"deleted": True}), schedule_callable=schedule
        )
        == 1
    )
    schedule.assert_awaited_once()
    async with factory() as session:
        row = await session.get(EnterpriseIdentitySource, source.id)
        assert row.last_synced_at is None and row.full_sync_requested_at is not None
        group = await session.scalar(select(EnterpriseDirectoryGroup))
        assert group.status == EnterpriseDirectoryEntryStatus.DISABLED


async def test_late_leave_uses_current_authoritative_user_not_old_payload(seeded):
    factory, _, _, ingest = await prepare(seeded)
    await ingest("late-leave", "user_leave_org")
    facts = {"user": {"id": "ou-member", "display_name": "Rejoined"}, "groups": [], "memberships": []}
    assert await process_dingtalk_directory_events_once(factory, fetch_callable=AsyncMock(return_value=facts)) == 1
    async with factory() as session:
        user = await session.scalar(select(EnterpriseDirectoryUser))
        assert user.status == EnterpriseDirectoryEntryStatus.ACTIVE


async def test_client_rechecks_scope_and_current_user_departments():
    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(responder)
    ) as client:
        facts = await client.fetch_user_event_facts("user-a")
    assert facts["user"]["id"] == "user-a"
    assert facts["memberships"] == [{"group_id": "2", "member_id": "user-a", "member_type": "user"}]
    assert facts["groups"][0]["parent_id"] is None


async def test_user_outside_authorized_scope_is_removed():
    def handler(request):
        if request.url.path.endswith("/v2/user/get"):
            return httpx.Response(
                200, json={"errcode": 0, "result": {"userid": "user-a", "active": True, "dept_id_list": []}}
            )
        return responder(request)

    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(handler)
    ) as client:
        assert await client.fetch_user_event_facts("user-a") == {"deleted": True}


@pytest.mark.parametrize("code,unavailable", [(33012, True), (60121, True), (60011, False), (-1, False)])
async def test_client_missing_user_and_permission_error_are_distinguished(code, unavailable):
    def handler(request):
        if request.url.path.endswith("/v2/user/get"):
            return httpx.Response(200, json={"errcode": code})
        return responder(request)

    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(handler)
    ) as client:
        if unavailable:
            assert await client.fetch_user_event_facts("user-a") == {"deleted": True}
        else:
            with pytest.raises(DingTalkIdentityError):
                await client.fetch_user_event_facts("user-a")


@pytest.mark.parametrize("code,deleted", [(40009, True), (60003, True), (60011, False)])
async def test_client_missing_department_and_permission_error_are_distinguished(code, deleted):
    def handler(request):
        if request.url.path.endswith("/v2/department/get"):
            return httpx.Response(200, json={"errcode": code})
        return responder(request)

    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(handler)
    ) as client:
        if deleted:
            assert await client.fetch_department_event_facts("2") == {"deleted": True}
        else:
            with pytest.raises(DingTalkIdentityError):
                await client.fetch_department_event_facts("2")


async def test_direct_imported_grant_stops_working_during_incremental_suspension(seeded):
    from dataclasses import replace
    from tests.test_external_knowledge_scope_sync import _snapshot
    from server.core.external_knowledge_scope_sync import ImportedGrant, import_external_knowledge_scope_snapshot
    from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope

    factory, session, source, ingest = await prepare(seeded)
    _, _, agent, member, resource = seeded
    snapshot = replace(
        _snapshot(),
        source_scope_type="document_read_discovery",
        source_scope_id="kb-a",
        grants=(ImportedGrant("user", "ou-member"),),
    )
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=snapshot)
    await session.commit()
    assert (await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)).allows(resource)
    await ingest("suspend-direct")
    async with factory() as read:
        assert not (await resolve_agent_knowledge_resource_scope(read, agent.id, member.id)).allows(resource)


async def test_legacy_manual_group_claim_cannot_bypass_suspended_directory(seeded):
    from server.models import ExternalUserPrincipalMembership, EnterpriseDirectoryPrincipalType
    from server.core.knowledge_subjects import (
        user_knowledge_subjects,
        batch_user_knowledge_subjects,
        knowledge_subject_members,
    )
    from server.enterprise_identity.principals import resolve_external_user_principals

    factory, session, source, ingest = await prepare(seeded)
    member = seeded[3]
    session.add(
        ExternalUserPrincipalMembership(
            identity_source_id=source.id,
            external_user_id="ou-member",
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
            principal_id="2",
        )
    )
    await session.commit()
    await ingest("suspend-manual")
    async with factory() as read:
        assert await resolve_external_user_principals(read, source.id, "ou-member") == set()
        assert ("IDENTITY_SOURCE", source.id) not in await user_knowledge_subjects(read, member.id)
        assert ("IDENTITY_SOURCE", source.id) not in (await batch_user_knowledge_subjects(read, [member.id]))[member.id]
        assert member.id not in set(
            await read.scalars(await knowledge_subject_members(read, ("IDENTITY_SOURCE", source.id)))
        )


async def test_http_conflict_mapping_is_friendly_and_domain_has_no_fastapi_dependency():
    from server.app import create_app
    from server.core.identity_source_space import IdentityDomainTenantConflict

    app = create_app()
    response = await app.exception_handlers[IdentityDomainTenantConflict](None, IdentityDomainTenantConflict())
    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["code"] == "DINGTALK_IDENTITY_DOMAIN_TENANT_CONFLICT"
    assert payload["detail"] == "DingTalk enterprise conflicts with this identity domain"


async def test_normal_disconnect_reconnects_without_error_counter(seeded, monkeypatch):
    from server.enterprise_identity import dingtalk_events as events
    from server.enterprise_identity import dingtalk_stream_runtime as runtime

    factory, _, source, _ = await prepare(seeded)
    monkeypatch.setattr(runtime, "stream_once", AsyncMock(side_effect=runtime.StreamReconnect()))
    config = verified_dingtalk_config(source)
    await events._listen(factory, source.id, "worker", config, source.tenant_id, dingtalk_config_digest(source, config))
    async with factory() as read:
        row = await read.get(EnterpriseIdentitySource, source.id)
        assert row.event_listener_status == FeishuEventSubscriptionStatus.CONNECTING
        assert row.event_listener_retry_count == 0


async def test_disabled_listener_runtime_and_backoff_are_collected(seeded):
    from server.enterprise_identity import dingtalk_events as events

    factory, session, source, _ = await prepare(seeded)
    source.status = "disabled"
    await session.commit()
    task = asyncio.create_task(asyncio.Event().wait())
    runtimes, retries = {source.id: task}, {source.id: 999999999}
    await events._reconcile_listeners(factory, "worker", runtimes, retries)
    assert not runtimes and not retries and task.cancelled()


async def test_department_facts_pagination_filters_inactive_and_detects_incomplete_page():
    pages = []

    def handler(request):
        if request.url.path.endswith("/v2/user/list"):
            cursor = json.loads(request.content)["cursor"]
            pages.append(cursor)
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "result": {
                        "list": [
                            {"userid": "active-" + str(cursor), "name": "Active", "active": True},
                            {"userid": "inactive", "name": "Inactive", "active": False},
                        ],
                        "has_more": cursor == 0,
                        "next_cursor": 1,
                    },
                },
            )
        return responder(request)

    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(handler)
    ) as client:
        facts = await client.fetch_department_event_facts("2")
    assert pages == [0, 1]
    assert {u["id"] for u in facts["users"]} == {"active-0", "active-1"}
    assert facts["disabled_users"] == ["inactive"]
    assert facts["department"]["display_name"] == "Engineering"

    def repeated(request):
        if request.url.path.endswith("/v2/user/list"):
            return httpx.Response(200, json={"errcode": 0, "result": {"list": [], "has_more": True, "next_cursor": 0}})
        return responder(request)

    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(repeated)
    ) as client:
        with pytest.raises(DingTalkIdentityError, match="REPEATED_DIRECTORY_CURSOR"):
            await client.fetch_department_event_facts("2")


async def test_active_full_sync_defers_event_processing(seeded):
    factory, session, source, ingest = await prepare(seeded)
    await ingest("full-sync")
    source.sync_kind = "full"
    source.sync_worker_id = "full-worker"
    source.sync_lease_until = datetime.now(UTC) + timedelta(minutes=1)
    await session.commit()
    fetch = AsyncMock()
    assert await process_dingtalk_directory_events_once(factory, fetch_callable=fetch) == 0
    fetch.assert_not_called()


async def test_config_replaced_during_fetch_is_fenced(seeded):
    factory, _, source, ingest = await prepare(seeded)
    await ingest("replace-config")

    async def fetch(*args, **kwargs):
        async with factory() as session:
            current = await session.get(EnterpriseIdentitySource, source.id)
            current.config_ciphertext = "new-configuration-version"
            await session.commit()
        return {"user": {"id": "ou-member", "display_name": "Stale"}, "groups": [], "memberships": []}

    assert await process_dingtalk_directory_events_once(factory, fetch_callable=fetch) == 0
    async with factory() as session:
        user = await session.scalar(select(EnterpriseDirectoryUser))
        assert user.status == EnterpriseDirectoryEntryStatus.DISABLED


async def test_ancestor_with_pending_department_event_is_not_reactivated_by_user_event(seeded):
    factory, _, source, ingest = await prepare(seeded)
    await ingest("pending-dept", "org_dept_modify", {"deptId": [2]})
    await ingest("pending-user")
    async with factory() as session:
        current = await session.get(EnterpriseIdentitySource, source.id)
        await apply_dingtalk_directory_event(
            session,
            current,
            event_type="user_modify_org",
            subject_id="ou-member",
            data={
                "user": {"id": "ou-member", "display_name": "Current"},
                "groups": [{"id": "2", "display_name": "Department", "parent_id": None}],
                "memberships": [{"group_id": "2", "member_id": "ou-member", "member_type": "user"}],
            },
        )
        group = await session.scalar(select(EnterpriseDirectoryGroup))
        assert group.status == EnterpriseDirectoryEntryStatus.DISABLED


async def test_missing_baseline_cannot_apply_pending_events(seeded):
    factory, session, source, ingest = await prepare(seeded)
    await ingest("baseline")
    source.last_synced_at = None
    await session.commit()
    fetch = AsyncMock()
    assert await process_dingtalk_directory_events_once(factory, fetch_callable=fetch) == 0
    fetch.assert_not_called()


async def test_batch_integrity_failure_is_nacked_instead_of_success(seeded, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    factory, _, _, ingest = await prepare(seeded)

    async def conflict(self):
        raise IntegrityError("injected conflict", {}, Exception("duplicate"))

    monkeypatch.setattr(AsyncSession, "commit", conflict)
    with pytest.raises(ValueError, match="DIRECTORY_EVENT_NOT_PERSISTED"):
        await ingest("conflict", data={"userId": ["ou-member", "other"]})
    async with factory() as session:
        assert await session.scalar(select(EnterpriseIdentitySourceEvent)) is None
        user = await session.scalar(select(EnterpriseDirectoryUser))
        assert user.status == EnterpriseDirectoryEntryStatus.ACTIVE


async def test_batch_ingest_round_trips_are_bounded(seeded):
    from sqlalchemy import event

    factory, session, _, ingest = await prepare(seeded)
    calls = []

    def before(*args):
        calls.append(1)

    event.listen(session.bind.sync_engine, "before_cursor_execute", before)
    try:
        assert await ingest("bounded-batch", data={"userId": ["user-" + str(i) for i in range(100)]})
        assert len(calls) <= 10
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", before)
    assert normalize_dingtalk_subjects("user_modify_org", {"userId": ["u"] * 101}) == [None]


@pytest.mark.parametrize("sync_kind,requested", [("full", False), ("groups", True)])
async def test_feishu_scope_event_preserves_full_drain_scheduling(seeded, sync_kind, requested):
    from server.enterprise_identity.feishu_events import process_feishu_directory_events_once

    factory, session, source, _ = await prepare(seeded)
    source.provider = IdentitySourceProvider.FEISHU
    source.sync_kind = sync_kind
    source.sync_worker_id = "drain-worker"
    source.sync_lease_until = datetime.now(UTC) + timedelta(minutes=1)
    session.add(EnterpriseIdentitySourceEvent(identity_source_id=source.id, event_type="contact.scope.updated_v3"))
    await session.commit()
    schedule = AsyncMock()
    assert (
        await process_feishu_directory_events_once(
            factory,
            fetch_callable=AsyncMock(return_value={}),
            apply_callable=AsyncMock(return_value=True),
            schedule_callable=schedule,
            draining_worker_id="drain-worker",
            event_cutoff=datetime.now(UTC),
        )
        == 1
    )
    async with factory() as read:
        current = await read.get(EnterpriseIdentitySource, source.id)
        assert (current.full_sync_requested_at is not None) == requested
