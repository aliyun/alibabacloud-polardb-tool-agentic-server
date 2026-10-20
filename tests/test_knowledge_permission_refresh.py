from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.crypto import encrypt
from server.core.external_knowledge_scope_sync import import_external_knowledge_scope_snapshot
from server.enterprise_identity import knowledge_refresh as refresh
from server.models import (
    Agent,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentKnowledgeScopeOrigin,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    IdentitySourceProvider,
    ExternalKnowledgeScopeSnapshot,
    KnowledgeResource,
    SystemConfig,
)
from tests.test_external_knowledge_scope_sync import _snapshot

pytest_plugins = ("tests._admin_api_fixtures", "tests.test_external_knowledge_scope_sync")


def config():
    return encrypt(
        json.dumps(
            {
                "app_id": "app",
                "app_secret": "secret",
                "acl_membership_snapshot": {
                    "dialect": "postgresql",
                    "host": "localhost",
                    "port": 5432,
                    "database": "meta",
                    "username": "readonly",
                    "password": "secret",
                },
            }
        )
    )


async def prepare(seeded):
    session, source, agent, member, resource = seeded
    source.config_ciphertext = config()
    await session.commit()
    return session, source, agent, member, resource, async_sessionmaker(session.bind, expire_on_commit=False)


def client_for(snapshots, callback=None, fail=False):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def iter_knowledge_scopes(self, **kwargs):
            if callback:
                await callback(kwargs)
            for snapshot in snapshots:
                yield replace(snapshot, tenant_key=kwargs["tenant_key"])
            if fail:
                raise RuntimeError("password=secret driver error")

        async def fetch_snapshot(self, **kwargs):
            raise AssertionError("No membership or directory reads")

    return Client


async def enqueue(session, agent, resource=None):
    result = await refresh.enqueue(
        session, agent.id, agent.created_by, resource_ids=[resource.id] if resource else None
    )
    await session.commit()
    return result["job_id"]


async def result(session, agent, job):
    aid = agent.id
    await session.rollback()
    await session.refresh(agent)
    return await refresh.status(session, aid, job)


async def test_agent_refresh_discovers_unbound_resources_and_runs(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    observed = []

    async def check(kwargs):
        observed.append(kwargs)
        # No PAS write transaction exists while pulling upstream pages.
        async with factory() as independent:
            row = await independent.get(SystemConfig, "test.upstream.can.write")
            assert row is None
            independent.add(SystemConfig(config_key="test.upstream.can.write", config_value="{}", config_version=1))
            await independent.commit()

    job = await enqueue(session, agent)
    await refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()], check))
    body = await result(session, agent, job)
    assert body["state"] == "SUCCEEDED"
    assert body["resources_completed"] == 1
    assert body["completed"][0]["bindings_projected"] == 1
    assert observed == [
        {
            "tenant_key": source.tenant_id,
            "space_id": resource.space_id,
            "kb_id": resource.kb_id,
            "max_grants_per_snapshot": refresh.MAX_GRANTS_PER_KB,
        }
    ]
    assert "owner" not in body and "targets" not in body
    assert (await session.get(EnterpriseIdentitySource, source.id)).last_synced_at == source.last_synced_at


@pytest.mark.parametrize(
    "mode,enabled", [(AgentKnowledgeScopeMode.LEGACY_ALL, True), (AgentKnowledgeScopeMode.SCOPED, False)]
)
async def test_rejects_unsupported_agent_modes(seeded, mode, enabled):
    session, source, agent, member, resource, factory = await prepare(seeded)
    agent.knowledge_scope_mode, agent.external_knowledge_auto_bind_enabled = mode, enabled
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await enqueue(session, agent)
    assert exc.value.status_code == 409


async def test_request_scope_validation_and_duplicate_job(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    for kwargs in ({"resource_ids": ["missing"]}, {"source_ids": ["missing"]}):
        with pytest.raises(HTTPException) as exc:
            await refresh.enqueue(session, agent.id, agent.created_by, **kwargs)
        assert exc.value.status_code == 422
        await session.rollback()
        await session.refresh(agent)
    job = await enqueue(session, agent)
    with pytest.raises(HTTPException) as exc:
        await enqueue(session, agent)
    assert exc.value.detail["job_id"] == job
    await session.rollback()
    with pytest.raises(HTTPException) as exc:
        await refresh.status(session, "different-agent", job)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("failure", ["empty", "stream", "stale", "unexpected", "capacity"])
async def test_failed_fetch_or_publish_retains_committed_bindings(seeded, failure, monkeypatch):
    session, source, agent, member, resource, factory = await prepare(seeded)
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    prior = list((await session.execute(select(AgentKnowledgeScopeBinding.id))).scalars())
    job = await enqueue(session, agent, resource)
    snapshot = replace(_snapshot(), generation="g2", fingerprint="g2")  # same timestamp rejects stale generation
    snapshots = [] if failure == "empty" else [snapshot]
    if failure == "unexpected":
        snapshots = [replace(snapshot, kb_id="wrong-kb")]
    if failure == "capacity":
        monkeypatch.setattr(refresh, "MAX_GRANTS_PER_KB", 0)
    await refresh.run(job, session_factory=factory, client_factory=client_for(snapshots, fail=failure == "stream"))
    body = await result(session, agent, job)
    assert body["state"] == "FAILED" and body["resources_completed"] == 0
    assert "secret" not in json.dumps(body)
    assert list((await session.execute(select(AgentKnowledgeScopeBinding.id))).scalars()) == prior
    assert (await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a")).generation == "g1"


async def test_publication_rechecks_mode_and_configuration(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)

    async def changed(kwargs):
        async with factory() as other:
            row = await other.get(Agent, agent.id)
            row.knowledge_scope_mode = AgentKnowledgeScopeMode.LEGACY_ALL
            await other.commit()

    job = await enqueue(session, agent, resource)
    await refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()], changed))
    assert (await result(session, agent, job))["state"] == "FAILED"
    assert not list(await session.scalars(select(AgentKnowledgeScopeBinding.id)))


async def test_agent_kb_isolation_manual_bindings_and_obsolete_cleanup(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    other = Agent(
        name="other",
        created_by=agent.created_by,
        knowledge_scope_mode=AgentKnowledgeScopeMode.SCOPED,
        external_knowledge_auto_bind_enabled=True,
    )
    second = KnowledgeResource(
        knowledge_space_id=resource.knowledge_space_id,
        polarrag_instance_id=resource.polarrag_instance_id,
        space_id=resource.space_id,
        kb_id="kb-b",
        name="B",
        kb_type=resource.kb_type,
        identity_domain=resource.identity_domain,
        binding_mode=resource.binding_mode,
        sync_status=resource.sync_status,
    )
    session.add_all([other, second])
    await session.flush()
    session.add_all(
        [
            AgentPolarRAGInstanceBinding(
                agent_id=other.id,
                polarrag_instance_id=resource.polarrag_instance_id,
                created_by_user_id=agent.created_by,
            ),
            AgentUserAssignment(agent_id=other.id, user_id=member.id, created_by_user_id=agent.created_by),
        ]
    )
    await session.flush()
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await import_external_knowledge_scope_snapshot(
        session, identity_source_id=source.id, snapshot=replace(_snapshot(), snapshot_id="snapshot-b", kb_id="kb-b")
    )
    manual = AgentKnowledgeScope(
        agent_id=agent.id,
        name="manual",
        origin=AgentKnowledgeScopeOrigin.MANUAL,
        knowledge_resource_ids_json=json.dumps([resource.id]),
        created_by_user_id=agent.created_by,
    )
    session.add(manual)
    await session.flush()
    binding = AgentKnowledgeScopeBinding.for_user(scope_id=manual.id, user_id=member.id)
    session.add(binding)
    await session.commit()
    binding_id, other_id = binding.id, other.id
    untouched = list(
        (
            await session.execute(
                select(AgentKnowledgeScopeBinding.id)
                .join(AgentKnowledgeScope, AgentKnowledgeScope.id == AgentKnowledgeScopeBinding.scope_id)
                .where(AgentKnowledgeScope.agent_id == other.id)
            )
        ).scalars()
    )
    new = replace(_snapshot(), snapshot_id="replacement", grants=(), status="DENY_ALL")
    job = await enqueue(session, agent, resource)
    await refresh.run(job, session_factory=factory, client_factory=client_for([new]))
    assert (await result(session, agent, job))["state"] == "SUCCEEDED"
    assert await session.get(AgentKnowledgeScopeBinding, binding_id)
    assert (
        list(
            (
                await session.execute(
                    select(AgentKnowledgeScopeBinding.id)
                    .join(AgentKnowledgeScope, AgentKnowledgeScope.id == AgentKnowledgeScopeBinding.scope_id)
                    .where(AgentKnowledgeScope.agent_id == other_id)
                )
            ).scalars()
        )
        == untouched
    )
    assert await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-b")
    assert await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a") is None


async def test_multiple_source_publish_rolls_back_as_one_kb(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    extra = EnterpriseIdentitySource.create(
        name="other source", provider=IdentitySourceProvider.FEISHU, tenant_id="tenant-b"
    )
    extra.status, extra.last_synced_at, extra.config_ciphertext = source.status, source.last_synced_at, config()
    session.add(extra)
    await session.flush()
    session.add(
        EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=extra.id, knowledge_space_id=resource.knowledge_space_id
        )
    )
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    job = await enqueue(session, agent, resource)
    # Both sources return the same snapshot ID, which violates source isolation.
    # Neither source may be committed when the second publication fails.
    await refresh.run(
        job,
        session_factory=factory,
        client_factory=client_for(
            [
                replace(
                    _snapshot(), generation="g2", fingerprint="g2", source_updated_at=datetime(2026, 9, 21, tzinfo=UTC)
                )
            ]
        ),
    )
    assert (await result(session, agent, job))["state"] == "FAILED"
    assert (await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a")).generation == "g1"


async def test_active_lease_prevents_duplicate_worker_and_expired_lease_resumes(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    job = await enqueue(session, agent, resource)
    assert await refresh._claim(factory, job)
    await refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()]))
    assert (await result(session, agent, job))["state"] == "RUNNING"
    row = await session.get(SystemConfig, refresh.PREFIX + job)
    value = json.loads(row.config_value)
    value["lease_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await refresh.write(session, row, value)
    await session.commit()
    await refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()]))
    assert (await result(session, agent, job))["state"] == "SUCCEEDED"


async def test_route_contract_and_admin_guard(client):
    http, _, member_headers = client
    response = await http.post(
        "/api/admin/agents/missing/knowledge-permissions/refresh", headers=member_headers, json={}
    )
    assert response.status_code == 403
    response = await http.get("/api/admin/agents/missing/knowledge-permissions/refresh/missing", headers=member_headers)
    assert response.status_code == 403


async def test_api_enqueues_202_without_waiting_for_upstream(seeded):
    from server.api.external_knowledge_scopes import (
        refresh_agent_knowledge_permissions,
        KnowledgePermissionRefreshRequest,
    )

    session, source, agent, member, resource, factory = await prepare(seeded)
    tasks = BackgroundTasks()
    body = await refresh_agent_knowledge_permissions(
        agent.id, KnowledgePermissionRefreshRequest(knowledge_resource_ids=[resource.id]), tasks, member, session
    )
    assert body["state"] == "QUEUED" and len(tasks.tasks) == 1


async def test_restart_resumes_only_unfinished_kbs(seeded, monkeypatch):
    session, source, agent, member, resource, factory = await prepare(seeded)
    second = KnowledgeResource(
        knowledge_space_id=resource.knowledge_space_id,
        polarrag_instance_id=resource.polarrag_instance_id,
        space_id=resource.space_id,
        kb_id="kb-b",
        name="B",
        kb_type=resource.kb_type,
        identity_domain=resource.identity_domain,
        binding_mode=resource.binding_mode,
        sync_status=resource.sync_status,
    )
    session.add(second)
    await session.commit()
    observed = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def iter_knowledge_scopes(self, **kwargs):
            observed.append(kwargs["kb_id"])
            yield replace(_snapshot(), snapshot_id=kwargs["kb_id"], kb_id=kwargs["kb_id"])

    original = refresh._publish

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError()

    job = await enqueue(session, agent)
    monkeypatch.setattr(refresh, "_publish", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await refresh.run(job, session_factory=factory, client_factory=Client)
    body = await result(session, agent, job)
    assert body["resources_completed"] == 1 and body["state"] == "RUNNING"
    row = await session.get(SystemConfig, refresh.PREFIX + job)
    value = json.loads(row.config_value)
    value["lease_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await refresh.write(session, row, value)
    await session.commit()
    monkeypatch.setattr(refresh, "_publish", original)
    await refresh.run(job, session_factory=factory, client_factory=Client)
    body = await result(session, agent, job)
    assert body["state"] == "SUCCEEDED" and body["resources_completed"] == 2
    assert sorted(observed) == ["kb-a", "kb-b"]


async def test_large_grants_bound_subject_query_parameters(seeded):
    from sqlalchemy import event
    from server.core.external_knowledge_scope_sync import ImportedGrant

    session, source, agent, member, resource, factory = await prepare(seeded)
    batches = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "user_external_identities.external_subject IN" in statement:
            batches.append(len(parameters))

    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        snapshot = replace(
            _snapshot(),
            grants=tuple(
                [ImportedGrant("user", "ou-member")] + [ImportedGrant("user", "other-" + str(i)) for i in range(1200)]
            ),
        )
        job = await enqueue(session, agent, resource)
        await refresh.run(job, session_factory=factory, client_factory=client_for([snapshot]))
        body = await result(session, agent, job)
        assert body["state"] == "SUCCEEDED"
        assert body["completed"][0]["grants_fetched"] == 1201
        assert body["completed"][0]["bindings_projected"] == 1
        assert len(batches) == 3 and max(batches) <= 501
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)


async def test_configuration_change_during_fetch_fences_publication(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    sid = source.id

    async def change(kwargs):
        async with factory() as other:
            row = await other.get(EnterpriseIdentitySource, sid)
            row.config_ciphertext = encrypt(json.dumps({"acl_membership_snapshot": {"host": "changed"}}))
            await other.commit()

    job = await enqueue(session, agent, resource)
    await refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()], change))
    assert (await result(session, agent, job))["state"] == "FAILED"
    assert await session.get(ExternalKnowledgeScopeSnapshot, "snapshot-a") is None


async def test_same_generation_reuses_snapshot_grants_but_recomputes_agent(seeded):
    from sqlalchemy import event

    session, source, agent, member, resource, factory = await prepare(seeded)
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    changes = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(("INSERT", "DELETE")) and "external_knowledge_scope_grants" in statement:
            changes.append(statement)

    event.listen(session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        job = await enqueue(session, agent, resource)
        await refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()]))
        body = await result(session, agent, job)
        assert body["state"] == "SUCCEEDED"
        assert body["completed"][0]["bindings_projected"] == 1
        assert not changes
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("terminal,code", [("SUCCEEDED", 200), ("FAILED", 502), ("PARTIAL", 502), ("RUNNING", 202)])
async def test_api_wait_result(seeded, monkeypatch, terminal, code):
    from server.api.external_knowledge_scopes import (
        refresh_agent_knowledge_permissions,
        KnowledgePermissionRefreshRequest,
    )

    session, source, agent, member, resource, factory = await prepare(seeded)
    started = []
    monkeypatch.setattr(refresh, "start", started.append)

    async def wait(aid, jid, seconds):
        assert aid == agent.id and seconds == 1 and started == [jid]
        return {"job_id": jid, "state": terminal}

    monkeypatch.setattr(refresh, "wait_for_result", wait)
    tasks = BackgroundTasks()
    result = await refresh_agent_knowledge_permissions(
        agent.id,
        KnowledgePermissionRefreshRequest(knowledge_resource_ids=[resource.id], wait_timeout_seconds=1),
        tasks,
        member,
        session,
    )
    assert result.status_code == code and not tasks.tasks


async def test_wait_observes_worker_and_timeout_does_not_cancel(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    job = await enqueue(session, agent)
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def delayed(_):
        entered.set()
        await gate.wait()

    worker = asyncio.create_task(
        refresh.run(job, session_factory=factory, client_factory=client_for([_snapshot()], delayed))
    )
    await asyncio.wait_for(entered.wait(), 2)
    result = await refresh.wait_for_result(agent.id, job, 0.02, session_factory=factory)
    assert result["state"] in {"QUEUED", "RUNNING"} and not worker.done()
    waiter = asyncio.create_task(refresh.wait_for_result(agent.id, job, 1, session_factory=factory))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not worker.done()
    gate.set()
    await worker
    assert (await refresh.wait_for_result(agent.id, job, 1, session_factory=factory))["state"] == "SUCCEEDED"


async def test_wait_requires_one_explicit_kb(seeded):
    from server.api.external_knowledge_scopes import (
        refresh_agent_knowledge_permissions,
        KnowledgePermissionRefreshRequest,
    )

    session, source, agent, member, resource, factory = await prepare(seeded)
    with pytest.raises(HTTPException) as exc:
        await refresh_agent_knowledge_permissions(
            agent.id, KnowledgePermissionRefreshRequest(wait_timeout_seconds=1), BackgroundTasks(), member, session
        )
    assert exc.value.status_code == 422
    from pydantic import ValidationError

    for seconds in (-1, 31, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            KnowledgePermissionRefreshRequest(wait_timeout_seconds=seconds)


@pytest.mark.parametrize("explicit_multiple", [False, True])
async def test_multi_kb_skips_untracked_missing_and_refreshes_later_kb(seeded, explicit_multiple):
    session, source, agent, member, resource, factory = await prepare(seeded)
    missing = KnowledgeResource(
        id="00000000-0000-0000-0000-000000000001",
        knowledge_space_id=resource.knowledge_space_id,
        polarrag_instance_id=resource.polarrag_instance_id,
        space_id=resource.space_id,
        kb_id="missing-kb",
        name="Missing",
        kb_type=resource.kb_type,
        identity_domain=resource.identity_domain,
        binding_mode=resource.binding_mode,
        sync_status=resource.sync_status,
    )
    manual = AgentKnowledgeScope(
        agent_id=agent.id, name="manual", origin="MANUAL", knowledge_resource_ids_json=json.dumps([missing.id])
    )
    session.add_all([missing, manual])
    await session.flush()
    binding = AgentKnowledgeScopeBinding(scope_id=manual.id, user_id=member.id)
    session.add(binding)
    await session.commit()
    binding_id = binding.id
    result = await refresh.enqueue(
        session, agent.id, agent.created_by, resource_ids=[missing.id, resource.id] if explicit_multiple else None
    )
    await session.commit()

    class Client:
        def __init__(self, **kwargs):
            pass

        async def iter_knowledge_scopes(self, **kwargs):
            if kwargs["kb_id"] == resource.kb_id:
                yield replace(_snapshot(), tenant_key=kwargs["tenant_key"])

    await refresh.run(result["job_id"], session_factory=factory, client_factory=Client)
    body = await refresh.status(session, agent.id, result["job_id"])
    assert body["state"] == "SUCCEEDED" and body["resources_completed"] == 2 and body["resources_skipped"] == 1
    assert body["completed"][0]["outcome"] == "SKIPPED" and body["completed"][1]["bindings_projected"] == 1
    assert await session.get(AgentKnowledgeScopeBinding, binding_id) is not None


async def test_whole_refresh_never_skips_previously_imported_missing_snapshot(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    job = await enqueue(session, agent)
    await refresh.run(job, session_factory=factory, client_factory=client_for([]))
    body = await refresh.status(session, agent.id, job)
    assert body["state"] == "FAILED" and body["error"] == "REFRESH_SNAPSHOT_MISSING" and body["resources_skipped"] == 0


async def test_new_refresh_reclaims_previous_terminal_job(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    first = await enqueue(session, agent, resource)
    await refresh.run(first, session_factory=factory, client_factory=client_for([_snapshot()]))
    assert (await result(session, agent, first))["state"] == "SUCCEEDED"
    second = await enqueue(session, agent, resource)
    with pytest.raises(HTTPException) as exc:
        await refresh.status(session, agent.id, first)
    assert exc.value.status_code == 404
    assert (await refresh.status(session, agent.id, second))["state"] == "QUEUED"


async def test_source_configuration_failure_has_stable_code(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    source.tenant_id = None
    with pytest.raises(refresh.RefreshPreconditionError) as exc:
        refresh._configuration(source)
    assert exc.value.code == "REFRESH_SOURCE_CHANGED"


def test_worker_limit_is_scoped_to_each_event_loop():
    async def saturate():
        semaphore = refresh._worker_semaphore()
        assert refresh._worker_semaphore() is semaphore
        gate = asyncio.Event()
        entered = 0

        async def worker():
            nonlocal entered
            async with semaphore:
                entered += 1
                await gate.wait()

        tasks = [asyncio.create_task(worker()) for _ in range(3)]
        await asyncio.sleep(0)
        assert entered == 2
        gate.set()
        await asyncio.gather(*tasks)
        assert entered == 3

    for _ in range(2):
        asyncio.run(saturate())


@pytest.mark.parametrize("error,expected", [
    ("REFRESH_AGENT_CHANGED", 409),
    ("REFRESH_RESOURCE_CHANGED", 409),
    ("REFRESH_SOURCE_CHANGED", 409),
    ("REFRESH_CAPACITY_LIMIT", 413),
    ("REFRESH_TIMEOUT", 502),
])
async def test_wait_http_status_distinguishes_conflict_capacity_and_upstream(seeded, monkeypatch, error, expected):
    from server.api.external_knowledge_scopes import refresh_agent_knowledge_permissions, KnowledgePermissionRefreshRequest
    session, source, agent, member, resource, factory = await prepare(seeded)
    monkeypatch.setattr(refresh, "start", lambda job_id: None)

    async def wait(*args):
        return {"state": "FAILED", "error": error}

    monkeypatch.setattr(refresh, "wait_for_result", wait)
    response = await refresh_agent_knowledge_permissions(
        agent.id, KnowledgePermissionRefreshRequest(knowledge_resource_ids=[resource.id], wait_timeout_seconds=1),
        BackgroundTasks(), member, session,
    )
    assert response.status_code == expected


async def test_waiting_backs_off_database_reads(seeded, monkeypatch):
    session, source, agent, member, resource, factory = await prepare(seeded)
    job = await enqueue(session, agent)
    calls = 0
    original = refresh.status

    async def counted(*args):
        nonlocal calls
        calls += 1
        return await original(*args)

    monkeypatch.setattr(refresh, "status", counted)
    body = await refresh.wait_for_result(agent.id, job, 0.8, session_factory=factory)
    assert body["state"] == "QUEUED"
    assert 3 <= calls <= 5


@pytest.fixture
async def persisted_seeded(seeded, tmp_path):
    # Concurrent polling needs independent SQLite connections, as on the deployed WAL database.
    origin, *entities = seeded
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "pas.db"))
    try:
        async with origin.bind.connect() as source, engine.connect() as target:
            source_driver = (await source.get_raw_connection()).driver_connection
            target_driver = (await target.get_raw_connection()).driver_connection
            await source_driver.backup(target_driver)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            copied = [await session.get(type(entity), entity.id) for entity in entities]
            yield session, *copied
    finally:
        await engine.dispose()


@pytest.mark.parametrize("use_pkb", [False, True])
async def test_single_untracked_missing_returns_success_during_bounded_wait(persisted_seeded, monkeypatch, use_pkb):
    from server.api.external_knowledge_scopes import refresh_agent_knowledge_permissions, KnowledgePermissionRefreshRequest
    session, source, agent, member, resource, factory = await prepare(persisted_seeded)
    manual = AgentKnowledgeScope(
        agent_id=agent.id, name="manual preserved", origin="MANUAL", knowledge_resource_ids_json=json.dumps([resource.id])
    )
    session.add(manual)
    await session.flush()
    binding = AgentKnowledgeScopeBinding(scope_id=manual.id, user_id=member.id)
    session.add(binding)
    await session.commit()
    binding_id = binding.id
    tasks = []
    original_wait = refresh.wait_for_result
    def start(job_id):
        tasks.append(asyncio.create_task(refresh.run(job_id, session_factory=factory, client_factory=client_for([]))))
    async def wait(aid, jid, seconds):
        return await original_wait(aid, jid, seconds, session_factory=factory)
    monkeypatch.setattr(refresh, "start", start)
    monkeypatch.setattr(refresh, "wait_for_result", wait)
    selectors = {"kb_ids": [resource.kb_id]} if use_pkb else {"knowledge_resource_ids": [resource.id]}
    response = await refresh_agent_knowledge_permissions(
        agent.id, KnowledgePermissionRefreshRequest(**selectors, wait_timeout_seconds=10),
        BackgroundTasks(), member, session,
    )
    await asyncio.gather(*tasks)
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["state"] == "SUCCEEDED" and body["resources_completed"] == body["resources_total"] == 1
    assert body["resources_skipped"] == 1 and body["error"] is None
    assert body["completed"][0]["reason"] == "NO_IMPORTED_OR_UPSTREAM_PERMISSION_SNAPSHOT"
    await session.rollback()
    assert await session.get(AgentKnowledgeScopeBinding, binding_id) is not None


async def test_single_missing_never_skips_existing_derived_scope_without_imported_header(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    scope = AgentKnowledgeScope(
        agent_id=agent.id, name="tracked scope", identity_source_id=source.id,
        origin="EXTERNAL_SYNC", knowledge_resource_ids_json=json.dumps([resource.id]),
    )
    session.add(scope)
    await session.flush()
    binding = AgentKnowledgeScopeBinding(scope_id=scope.id, user_id=member.id)
    session.add(binding)
    await session.commit()
    binding_id = binding.id
    job = await enqueue(session, agent, resource)
    await refresh.run(job, session_factory=factory, client_factory=client_for([]))
    body = await result(session, agent, job)
    assert body["state"] == "FAILED" and body["error"] == "REFRESH_SNAPSHOT_MISSING"
    assert body["resources_skipped"] == 0
    assert await session.get(AgentKnowledgeScopeBinding, binding_id) is not None


async def test_single_untracked_connection_failure_is_not_reported_as_skip(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    job = await enqueue(session, agent, resource)
    await refresh.run(job, session_factory=factory, client_factory=client_for([], fail=True))
    body = await result(session, agent, job)
    assert body["state"] == "FAILED" and body["resources_skipped"] == 0


async def test_single_complete_zero_grant_snapshot_is_published_not_skipped(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    assert list(await session.scalars(select(AgentKnowledgeScopeBinding.id)))
    empty = replace(
        _snapshot(), generation="g2-empty", fingerprint="g2-empty", grants=(),
        source_updated_at=datetime.now(UTC),
    )
    job = await enqueue(session, agent, resource)
    await refresh.run(job, session_factory=factory, client_factory=client_for([empty]))
    body = await result(session, agent, job)
    assert body["state"] == "SUCCEEDED" and body["resources_skipped"] == 0
    assert body["completed"][0]["grants_fetched"] == 0
    assert not list(await session.scalars(select(AgentKnowledgeScopeBinding.id)))


@pytest.mark.parametrize('missing_provider', ['feishu', 'dingtalk'])
@pytest.mark.parametrize('tracked', ['none', 'snapshot', 'derived'])
async def test_mixed_provider_refresh_preserves_tracked_missing_sources(seeded, missing_provider, tracked):
    from server.core.crypto import decrypt
    from server.enterprise_identity.dingtalk_auth import dingtalk_config_digest
    from server.models import EnterpriseIdentitySourceStatus

    session, feishu, agent, member, resource, factory = await prepare(seeded)
    ding = EnterpriseIdentitySource.create(name='DingTalk refresh', provider=IdentitySourceProvider.DINGTALK,
                                          tenant_id='ding-tenant')
    cfg = json.loads(decrypt(config()))
    cfg.update(client_id='ding-app', client_secret='ding-secret')
    cfg['tenant_verified_digest'] = dingtalk_config_digest(ding, cfg)
    ding.config_ciphertext = encrypt(json.dumps(cfg))
    ding.status = EnterpriseIdentitySourceStatus.ACTIVE
    ding.last_synced_at = datetime.now(UTC)
    session.add(ding)
    await session.flush()
    session.add(EnterpriseIdentitySourceSpaceBinding(identity_source_id=ding.id,
                                                    knowledge_space_id=resource.knowledge_space_id))
    snapshots = {
        feishu.id: _snapshot(),
        ding.id: replace(_snapshot(), snapshot_id='ding-snapshot', tenant_key=ding.tenant_id,
                         source_scope_type='document_read_discovery', source_scope_id=resource.kb_id),
    }
    missing = feishu if missing_provider == 'feishu' else ding
    present = ding if missing_provider == 'feishu' else feishu
    retained_binding = None
    if tracked == 'snapshot':
        await import_external_knowledge_scope_snapshot(session, identity_source_id=missing.id,
                                                       snapshot=snapshots[missing.id])
    elif tracked == 'derived':
        scope = AgentKnowledgeScope(agent_id=agent.id, identity_source_id=missing.id, name='Retained',
                                    origin=AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
                                    knowledge_resource_ids_json=json.dumps([resource.id]))
        session.add(scope)
        await session.flush()
        binding = AgentKnowledgeScopeBinding(scope_id=scope.id, user_id=member.id)
        session.add(binding)
        await session.flush()
        retained_binding = binding.id
    await session.commit()
    job = await enqueue(session, agent, resource)
    present_snapshot = snapshots[present.id]
    present_tenant = present.tenant_id
    present_id, missing_id = present.id, missing.id

    class Client:
        def __init__(self, **kwargs):
            pass

        async def iter_knowledge_scopes(self, **kwargs):
            if kwargs['tenant_key'] == present_tenant:
                yield replace(present_snapshot, tenant_key=present_tenant)

    await refresh.run(job, session_factory=factory, client_factory=Client)
    body = await result(session, agent, job)
    if tracked == 'none':
        assert body['state'] == 'SUCCEEDED'
        assert body['completed'][0]['snapshots_processed'] == 1
        assert body['completed'][0]['identity_sources_skipped'] == [missing_id]
        assert await session.scalar(select(ExternalKnowledgeScopeSnapshot.id).where(
            ExternalKnowledgeScopeSnapshot.identity_source_id == present_id))
    else:
        assert body['state'] == 'FAILED' and body['error'] == 'REFRESH_SNAPSHOT_MISSING'
        # A missing tracked source cannot partially publish another source.
        assert await session.scalar(select(ExternalKnowledgeScopeSnapshot.id).where(
            ExternalKnowledgeScopeSnapshot.identity_source_id == present_id)) is None
        if retained_binding:
            assert await session.get(AgentKnowledgeScopeBinding, retained_binding) is not None
        else:
            assert await session.scalar(select(ExternalKnowledgeScopeSnapshot.id).where(
                ExternalKnowledgeScopeSnapshot.identity_source_id == missing_id))


async def test_missing_source_imported_after_pull_is_checked_before_publication(seeded, monkeypatch):
    session, source, agent, member, resource, factory = await prepare(seeded)
    extra = EnterpriseIdentitySource.create(name='Racing source', provider=IdentitySourceProvider.FEISHU,
                                           tenant_id='racing-tenant')
    extra.status, extra.last_synced_at, extra.config_ciphertext = source.status, source.last_synced_at, config()
    session.add(extra)
    await session.flush()
    session.add(EnterpriseIdentitySourceSpaceBinding(identity_source_id=extra.id,
                                                    knowledge_space_id=resource.knowledge_space_id))
    await session.commit()
    job = await enqueue(session, agent, resource)
    present_id, extra_id, present_tenant = source.id, extra.id, source.tenant_id
    original = refresh._publish
    raced = False

    class Client:
        def __init__(self, **kwargs):
            pass

        async def iter_knowledge_scopes(self, **kwargs):
            if kwargs['tenant_key'] == present_tenant:
                yield replace(_snapshot(), tenant_key=present_tenant)

    async def publish(*args, **kwargs):
        nonlocal raced
        async with factory() as other_session:
            await import_external_knowledge_scope_snapshot(
                other_session, identity_source_id=extra_id,
                snapshot=replace(_snapshot(), snapshot_id='racing-header', tenant_key='racing-tenant'),
            )
            await other_session.commit()
        raced = True
        return await original(*args, **kwargs)

    monkeypatch.setattr(refresh, '_publish', publish)
    await refresh.run(job, session_factory=factory, client_factory=Client)
    body = await result(session, agent, job)
    assert raced
    assert body['state'] == 'FAILED' and body['error'] == 'REFRESH_SNAPSHOT_MISSING'
    assert await session.scalar(select(ExternalKnowledgeScopeSnapshot.id).where(
        ExternalKnowledgeScopeSnapshot.identity_source_id == extra_id))
    assert await session.scalar(select(ExternalKnowledgeScopeSnapshot.id).where(
        ExternalKnowledgeScopeSnapshot.identity_source_id == present_id)) is None
