from __future__ import annotations

from datetime import UTC, datetime
from contextlib import asynccontextmanager

import pytest

from server.enterprise_identity.acl_snapshot import FeishuAclMembershipSnapshotClient


class _Cursor:
    def __init__(self) -> None:
        self.statement: str | None = None
        self.params: tuple[object, ...] | None = None

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, statement: str, params: tuple[object, ...] | None = None) -> None:
        self.statement = statement
        self.params = params

    async def fetchall(self):
        assert self.statement is not None
        if "external_kb_scope_snapshots" in self.statement:
            return [
                (
                    "snapshot-1",
                    "tenant-a",
                    "wiki_space",
                    "wiki-1",
                    "Research Wiki",
                    "pks-1",
                    "pkb-1",
                    "generation-1",
                    "fingerprint-1",
                    "ACTIVE",
                    datetime(2026, 9, 20, tzinfo=UTC),
                    "user",
                    "ou_alice",
                )
            ]
        return [
            ("department", "od_research", "ou_alice"),
            ("group", "oc_user_group", "ou_alice"),
            ("chat", "oc_chat", "ou_alice"),
            ("acl_group", "polarrag/acl_group/feishu/tenant-a/hash", "ou_alice"),
            ("wiki_space", "wiki_ignored", "ou_alice"),
        ]


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    async def rollback(self) -> None:
        pass

    async def ensure_closed(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_feishu_acl_membership_snapshot_maps_document_principals_to_directory_groups():
    connection = _Connection()

    async def connect(**kwargs):
        assert kwargs == {
            "host": "meta.example.test",
            "port": 3306,
            "user": "readonly",
            "password": "secret",
            "db": "polar_rag_meta",
            "connect_timeout": 10,
            "autocommit": False,
        }
        return connection

    client = FeishuAclMembershipSnapshotClient(
        host="meta.example.test",
        port=3306,
        username="readonly",
        password="secret",
        connect=connect,
    )

    snapshot = await client.fetch_snapshot(tenant_id="tenant-a")

    assert connection.cursor_instance.params == ("FEISHU", "tenant-a", 1000)
    assert connection.closed is True
    assert snapshot.groups == [
        {"id": "oc_chat", "display_name": "oc_chat", "principal_type": "group"},
        {"id": "oc_user_group", "display_name": "oc_user_group", "principal_type": "group"},
        {"id": "od_research", "display_name": "od_research", "principal_type": "department"},
        {
            "id": "polarrag/acl_group/feishu/tenant-a/hash",
            "display_name": "polarrag/acl_group/feishu/tenant-a/hash",
            "principal_type": "acl_group",
        },
    ]
    assert snapshot.memberships == [
        {"group_id": "oc_chat", "member_type": "user", "member_id": "ou_alice"},
        {"group_id": "oc_user_group", "member_type": "user", "member_id": "ou_alice"},
        {"group_id": "od_research", "member_type": "user", "member_id": "ou_alice"},
        {
            "group_id": "polarrag/acl_group/feishu/tenant-a/hash",
            "member_type": "user",
            "member_id": "ou_alice",
        },
    ]


@pytest.mark.asyncio
async def test_feishu_acl_membership_snapshot_filters_one_user():
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    client = FeishuAclMembershipSnapshotClient(
        host="meta.example.test",
        port=3306,
        username="readonly",
        password="secret",
        connect=connect,
    )
    snapshot = await client.fetch_user_snapshot(tenant_id="tenant-a", user_id="ou_alice")
    assert connection.cursor_instance.params == ("FEISHU", "tenant-a", "ou_alice", 1000)
    assert "member_user_id = %s" in connection.cursor_instance.statement
    assert {group["id"] for group in snapshot.groups} == {
        "od_research",
        "oc_user_group",
        "oc_chat",
        "polarrag/acl_group/feishu/tenant-a/hash",
    }


@pytest.mark.asyncio
async def test_feishu_acl_membership_snapshot_includes_knowledge_scopes():
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    client = FeishuAclMembershipSnapshotClient(
        host="meta.example.test",
        port=3306,
        username="readonly",
        password="secret",
        connect=connect,
    )
    snapshot = await client.fetch_snapshot(tenant_id="tenant-a")

    scopes = [scope async for scope in snapshot.knowledge_scopes]
    assert len(scopes) == 1
    scope = scopes[0]
    assert (scope.snapshot_id, scope.space_id, scope.kb_id, scope.status) == (
        "snapshot-1",
        "pks-1",
        "pkb-1",
        "ACTIVE",
    )
    assert [(grant.subject_type, grant.subject_id) for grant in scope.grants] == [("user", "ou_alice")]


@pytest.mark.asyncio
async def test_feishu_acl_membership_snapshot_keeps_scope_without_grants():
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    original_fetchall = connection.cursor_instance.fetchall

    async def fetchall():
        if (
            connection.cursor_instance.statement
            and "external_kb_scope_snapshots" in connection.cursor_instance.statement
        ):
            row = list((await original_fetchall())[0])
            row[-2:] = [None, None]
            return [tuple(row)]
        return await original_fetchall()

    connection.cursor_instance.fetchall = fetchall
    client = FeishuAclMembershipSnapshotClient(
        host="meta.example.test",
        port=3306,
        username="readonly",
        password="secret",
        connect=connect,
    )

    snapshot = await client.fetch_snapshot(tenant_id="tenant-a")

    scopes = [scope async for scope in snapshot.knowledge_scopes]
    assert len(scopes) == 1
    assert scopes[0].grants == ()
    assert "LEFT JOIN external_kb_scope_grants" in (connection.cursor_instance.statement or "")


class _PostgresConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    @asynccontextmanager
    async def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        yield

    async def fetch(self, statement: str, *params: object):
        self.calls.append((statement, params))
        if "external_kb_scope_snapshots" in statement:
            return [
                (
                    "snapshot-1",
                    "tenant-a",
                    "wiki_space",
                    "wiki-1",
                    "Research Wiki",
                    "pks-1",
                    "pkb-1",
                    "generation-1",
                    "fingerprint-1",
                    "ACTIVE",
                    datetime(2026, 9, 20, tzinfo=UTC),
                    "user",
                    "ou_alice",
                )
            ]
        return [("department", "od_research", "ou_alice")]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_feishu_acl_snapshot_supports_postgresql_etl_database():
    connection = _PostgresConnection()

    async def connect(**kwargs):
        assert kwargs == {
            "host": "meta.example.test",
            "port": 5432,
            "user": "readonly",
            "password": "secret",
            "database": "polar_rag_meta",
            "timeout": 10,
            "command_timeout": 10,
        }
        return connection

    client = FeishuAclMembershipSnapshotClient(
        host="meta.example.test",
        port=5432,
        username="readonly",
        password="secret",
        dialect="postgresql",
        connect=connect,
    )

    snapshot = await client.fetch_snapshot(tenant_id="tenant-a")

    assert connection.closed is True
    scopes = [scope async for scope in snapshot.knowledge_scopes]
    assert len(connection.calls) == 2
    assert all(params == ("FEISHU", "tenant-a", 1000) for _, params in connection.calls)
    assert "LEFT JOIN external_kb_scope_grants" in connection.calls[1][0]
    assert snapshot.memberships == [{"group_id": "od_research", "member_type": "user", "member_id": "ou_alice"}]
    assert scopes[0].grants[0].subject_id == "ou_alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
async def test_paged_snapshot_keeps_each_scope_complete_and_closes_transactions(dialect):
    from server.enterprise_identity.acl_snapshot import AclMembershipSnapshotError

    memberships = [("department", "od_research", f"ou_{i}") for i in range(5)]
    head = (
        "a",
        "tenant-a",
        "wiki_space",
        "wiki-a",
        "Wiki",
        "pks",
        "pkb",
        "g1",
        "f1",
        "ACTIVE",
        datetime(2026, 9, 20, tzinfo=UTC),
    )
    scope_rows = [head + ("user", f"ou_{i}") for i in range(5)]
    scope_rows += [("b",) + head[1:] + (None, None)]
    calls = []
    connections = []
    fail_page = False

    class Connection:
        def __init__(self):
            self.closed = False
            self.rollback_called = False
            self.statement = ""
            self.params = ()
            self.scope_pages = 0
            self.view = list(scope_rows)

        @asynccontextmanager
        async def transaction(self, **kwargs):
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            yield

        @asynccontextmanager
        async def cursor(self):
            yield self

        async def execute(self, statement, params=()):
            self.statement, self.params = statement, params
            if not params:
                calls.append((statement, params))

        async def fetchall(self):
            return await self.fetch(self.statement, *self.params)

        async def fetch(self, statement, *params):
            calls.append((statement, params))
            assert "LIMIT" in statement and params[-1] == 2
            is_scope = "external_kb_scope_snapshots" in statement
            rows = self.view if is_scope else memberships
            key = (lambda row: (row[0], row[11] or "", row[12] or "")) if is_scope else tuple
            if len(params) == 6:
                rows = [row for row in rows if key(row) > params[2:5]]
            page = rows[: params[-1]]
            if is_scope:
                self.scope_pages += 1
                if fail_page and self.scope_pages == 2:
                    raise RuntimeError("read interrupted")
                # Another ETL transaction publishes g2; this reader keeps its g1 view.
                scope_rows[:] = [(row[0],) + head[1:7] + ("g2", "f2") + head[9:] + row[11:] for row in scope_rows]
            return page

        async def close(self):
            self.closed = True

        async def rollback(self):
            self.rollback_called = True

        async def ensure_closed(self):
            self.closed = True

    async def connect(**kwargs):
        connection = Connection()
        connections.append(connection)
        return connection

    client = FeishuAclMembershipSnapshotClient(
        host="meta",
        port=5432 if dialect == "postgresql" else 3306,
        username="readonly",
        password="secret",
        dialect=dialect,
        page_size=2,
        connect=connect,
    )
    snapshot = await client.fetch_snapshot(tenant_id="tenant-a")
    assert len(snapshot.memberships) == 5
    iterator = snapshot.knowledge_scopes.__aiter__()
    first = await anext(iterator)
    assert first.snapshot_id == "a" and len(first.grants) == 5 and first.generation == "g1"
    assert connections[-1].scope_pages == 3
    second = await anext(iterator)
    assert second.snapshot_id == "b" and second.grants == ()
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert all(connection.closed for connection in connections)
    if dialect == "mysql":
        assert all(connection.rollback_called for connection in connections)
        assert sum("CONSISTENT SNAPSHOT, READ ONLY" in statement for statement, _ in calls) == 2
    # A failed second page must not emit the first two grants as a complete scope.
    fail_page = True
    failing = client.iter_knowledge_scopes(tenant_key="tenant-a")
    with pytest.raises(AclMembershipSnapshotError, match="query failed"):
        await anext(failing)
    assert connections[-1].closed
    fail_page = False
    interrupted = client.iter_knowledge_scopes(tenant_key="tenant-a")
    await anext(interrupted)
    await interrupted.aclose()
    assert connections[-1].closed


@pytest.mark.asyncio
async def test_mysql_page_timeout_closes_connection_without_driver_packet_timeout(monkeypatch):
    import asyncio
    from server.enterprise_identity import acl_snapshot as module

    connection = _Connection()
    original_timeout = asyncio.timeout
    monkeypatch.setattr(module.asyncio, "timeout", lambda _seconds: original_timeout(0.01))
    original_execute = connection.cursor_instance.execute

    async def execute(statement, params=None):
        if statement.lstrip().startswith("SELECT"):
            await asyncio.Event().wait()
        await original_execute(statement, params)

    connection.cursor_instance.execute = execute

    async def connect(**kwargs):
        assert "read_timeout" not in kwargs
        return connection

    client = FeishuAclMembershipSnapshotClient(
        host="meta", port=3306, username="readonly", password="secret", connect=connect
    )
    with pytest.raises(module.AclMembershipSnapshotError, match="query failed"):
        await client.fetch_snapshot(tenant_id="tenant-a")
    assert connection.closed


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
async def test_filtered_kb_stream_pushes_parameters_into_every_page(dialect):
    calls = []
    head = ("filtered", "tenant", "wiki_space", "wiki", "name", "space", "kb", "g", "g", "ACTIVE", datetime.now(UTC))
    pages = [head + ("user", "a"), head + ("user", "b")]

    class Connection:
        @asynccontextmanager
        async def transaction(self, **kwargs):
            yield

        @asynccontextmanager
        async def cursor(self):
            yield self

        async def execute(self, statement, params=()):
            self.statement, self.params = statement, params

        async def fetchall(self):
            return await self.fetch(self.statement, *self.params)

        async def fetch(self, statement, *params):
            calls.append((statement, params))
            assert "snapshot.space_id = " in statement and "snapshot.kb_id = " in statement
            assert params[:4] == ("FEISHU", "tenant", "space", "kb")
            rows = [r for r in pages if len(params) == 5 or (r[0], r[11], r[12]) > params[4:7]]
            return rows[:params[-1]]

        async def close(self):
            pass

        async def rollback(self):
            pass

        async def ensure_closed(self):
            pass

    async def connect(**kwargs):
        return Connection()

    client = FeishuAclMembershipSnapshotClient(host="meta", port=5432 if dialect == "postgresql" else 3306,
        username="readonly", password="secret", dialect=dialect, page_size=1, connect=connect)
    scopes = [s async for s in client.iter_knowledge_scopes(tenant_key="tenant", space_id="space", kb_id="kb")]
    assert len(scopes) == 1 and len(scopes[0].grants) == 2
    assert len(calls) == 3


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
async def test_snapshot_capacity_rejects_before_emitting_and_closes(dialect):
    from server.enterprise_identity.acl_snapshot import AclMembershipSnapshotCapacityError
    connection = _Connection() if dialect == 'mysql' else _PostgresConnection()

    async def connect(**kwargs):
        return connection
    client = FeishuAclMembershipSnapshotClient(host='meta', port=3306 if dialect == 'mysql' else 5432,
        username='readonly', password='secret', dialect=dialect, connect=connect)
    stream = client.iter_knowledge_scopes(tenant_key='tenant-a', max_grants_per_snapshot=0)
    with pytest.raises(AclMembershipSnapshotCapacityError):
        await anext(stream)
    assert connection.closed
