from __future__ import annotations

import asyncio
import json

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any, Awaitable, Callable, cast

import asyncmy  # type: ignore[import-untyped]
import asyncpg  # type: ignore[import-untyped]


class AclMembershipSnapshotCapacityError(ValueError):
    """A complete snapshot cannot be buffered within the configured bound."""


class AclMembershipSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class AclMembershipSnapshot:
    groups: list[dict[str, str]]
    memberships: list[dict[str, str]]
    knowledge_scopes: tuple[ExternalKnowledgeScopeSnapshot, ...] | AsyncIterable[ExternalKnowledgeScopeSnapshot] = ()


@dataclass(frozen=True)
class ExternalKnowledgeScopeGrant:
    subject_type: str
    subject_id: str


@dataclass(frozen=True)
class ExternalKnowledgeScopeSnapshot:
    snapshot_id: str
    tenant_key: str
    source_scope_type: str
    source_scope_id: str
    source_scope_name: str
    space_id: str
    kb_id: str
    generation: str
    fingerprint: str
    status: str
    source_updated_at: datetime | None
    grants: tuple[ExternalKnowledgeScopeGrant, ...]


_PRINCIPAL_TYPES = {
    "department": "department",
    "group": "group",
    "chat": "group",
    "acl_group": "acl_group",
}

_MEMBERSHIP_SQL = """
    SELECT principal_type, principal_id, member_user_id
    FROM acl_principal_membership
    WHERE source = %s
      AND tenant_key = %s
      AND member_user_id_type = 'user_id'
      AND is_deleted = 0
"""

_KNOWLEDGE_SCOPE_SQL = """
    SELECT snapshot.snapshot_id, snapshot.tenant_key, snapshot.source_scope_type,
           snapshot.source_scope_id, snapshot.source_scope_name, snapshot.space_id,
           snapshot.kb_id, snapshot.generation, snapshot.fingerprint,
           snapshot.scope_status, snapshot.updated_at,
           scope_grant.subject_type, scope_grant.subject_id
    FROM external_kb_scope_snapshots AS snapshot
    LEFT JOIN external_kb_scope_grants AS scope_grant
      ON scope_grant.snapshot_id = snapshot.snapshot_id
    WHERE snapshot.source = {source_placeholder}
      AND snapshot.tenant_key = {tenant_placeholder}
"""

_SCOPE_STATUSES = frozenset({"ACTIVE", "DENY_ALL", "UNSUPPORTED"})
_SCOPE_SUBJECT_TYPES = frozenset({"user", "department", "chat", "group", "domain"})


class FeishuAclMembershipSnapshotClient:
    """Read ETL authoritative ACL projections for one registered provider."""

    source = "FEISHU"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        database: str = "polar_rag_meta",
        dialect: str = "mysql",
        page_size: int = 1000,
        connect: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        if not host.strip() or not username.strip() or not password:
            raise AclMembershipSnapshotError("ACL membership snapshot connection is incomplete")
        if not database.strip() or not 1 <= port <= 65535:
            raise AclMembershipSnapshotError("ACL membership snapshot connection is invalid")
        self._host = host.strip()
        self._port = port
        self._username = username.strip()
        self._password = password
        self._database = database.strip()
        normalized_dialect = dialect.strip().lower()
        if normalized_dialect not in {"mysql", "postgresql"}:
            raise AclMembershipSnapshotError("ACL membership snapshot dialect is invalid")
        self._dialect = normalized_dialect
        if not 1 <= page_size <= 5000:
            raise AclMembershipSnapshotError("ACL membership snapshot page size is invalid")
        self._page_size = page_size
        self._connect: Callable[..., Awaitable[Any]] = connect or cast(
            Callable[..., Awaitable[Any]],
            asyncpg.connect if normalized_dialect == "postgresql" else asyncmy.connect,
        )

    async def verify_connection(self, *, tenant_id: str) -> None:
        """Verify read access to the ETL ACL contract without importing facts."""
        async with self._read_connection() as connection:
            if self._dialect == "postgresql":
                await connection.fetch(_MEMBERSHIP_SQL.replace("%s", "$1", 1).replace("%s", "$2", 1) + " LIMIT 0", self.source, tenant_id)
                await connection.fetch(_KNOWLEDGE_SCOPE_SQL.format(source_placeholder="$1", tenant_placeholder="$2") + " LIMIT 0", self.source, tenant_id)
            else:
                async with connection.cursor() as cursor:
                    await cursor.execute(_MEMBERSHIP_SQL + " LIMIT 0", (self.source, tenant_id))
                    await cursor.execute(_KNOWLEDGE_SCOPE_SQL.format(source_placeholder="%s", tenant_placeholder="%s") + " LIMIT 0", (self.source, tenant_id))

    async def fetch_snapshot(self, *, tenant_id: str) -> AclMembershipSnapshot:
        return await self._fetch_snapshot(tenant_id=tenant_id, user_id=None)

    async def fetch_user_snapshot(self, *, tenant_id: str, user_id: str) -> AclMembershipSnapshot:
        if not user_id.strip():
            raise AclMembershipSnapshotError("Feishu user ID is required")
        return await self._fetch_snapshot(tenant_id=tenant_id, user_id=user_id.strip())

    async def _fetch_snapshot(self, *, tenant_id: str, user_id: str | None) -> AclMembershipSnapshot:
        tenant_key = tenant_id.strip()
        if not tenant_key:
            raise AclMembershipSnapshotError("Feishu tenant key is required")
        groups: dict[str, dict[str, str]] = {}
        memberships: set[tuple[str, str]] = set()
        async with aclosing(self._membership_rows(tenant_key, user_id)) as rows:
            async for raw_row in rows:
                row = tuple(raw_row)
                if (
                    not isinstance(row, (tuple, list))
                    or len(row) != 3
                    or not all(isinstance(value, str) for value in row)
                ):
                    raise AclMembershipSnapshotError("ACL membership snapshot row is invalid")
                source_type, principal_id, member_user_id = (value.strip() for value in row)
                principal_type = _PRINCIPAL_TYPES.get(source_type)
                if principal_type is None:
                    continue
                if not principal_id or not member_user_id:
                    raise AclMembershipSnapshotError("ACL membership snapshot row is incomplete")
                groups[principal_id] = {
                    "id": principal_id,
                    "display_name": principal_id,
                    "principal_type": principal_type,
                }
                memberships.add((principal_id, member_user_id))
        return AclMembershipSnapshot(
            groups=sorted(groups.values(), key=lambda item: item["id"]),
            memberships=[
                {"group_id": group_id, "member_type": "user", "member_id": member_id}
                for group_id, member_id in sorted(memberships)
            ],
            knowledge_scopes=self.iter_knowledge_scopes(tenant_key=tenant_key) if user_id is None else (),
        )

    @asynccontextmanager
    async def _read_connection(self) -> AsyncIterator[Any]:
        connection: Any = None
        try:
            if self._dialect == "postgresql":
                connection = await self._connect(
                    host=self._host,
                    port=self._port,
                    user=self._username,
                    password=self._password,
                    database=self._database,
                    timeout=10,
                    command_timeout=10,
                )
                async with connection.transaction(isolation="repeatable_read", readonly=True):
                    yield connection
            else:
                # Bound complete operations rather than asyncmy's per-packet
                # read_timeout, which can stall caching_sha2 fast authentication.
                connection = await asyncio.wait_for(
                    self._connect(
                        host=self._host,
                        port=self._port,
                        user=self._username,
                        password=self._password,
                        db=self._database,
                        connect_timeout=10,
                        autocommit=False,
                    ),
                    timeout=10,
                )
                async with asyncio.timeout(30):
                    async with connection.cursor() as cursor:
                        await cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                        await cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                yield connection
        except (AclMembershipSnapshotError, AclMembershipSnapshotCapacityError):
            raise
        except Exception as exc:
            raise AclMembershipSnapshotError("ACL membership snapshot query failed") from exc
        finally:
            if connection is not None:
                try:
                    if self._dialect == "postgresql":
                        await connection.close()
                    else:
                        try:
                            await asyncio.wait_for(connection.rollback(), timeout=5)
                        finally:
                            await asyncio.wait_for(connection.ensure_closed(), timeout=5)
                except Exception:
                    pass

    async def _page(self, connection: Any, statement: str, params: tuple[object, ...]) -> Any:
        if self._dialect == "postgresql":
            for index in range(1, len(params) + 1):
                statement = statement.replace("%s", f"${index}", 1)
            return await connection.fetch(statement, *params)
        async with asyncio.timeout(30):
            async with connection.cursor() as cursor:
                await cursor.execute(statement, params)
                return await cursor.fetchall()

    async def _membership_rows(self, tenant_key: str, user_id: str | None) -> AsyncIterator[Any]:
        last: tuple[object, ...] | None = None
        async with self._read_connection() as connection:
            while True:
                statement = _MEMBERSHIP_SQL
                params: tuple[object, ...] = (self.source, tenant_key)
                if user_id is not None:
                    statement += " AND member_user_id = %s"
                    params += (user_id,)
                if last is not None:
                    statement += " AND (principal_type, principal_id, member_user_id) > (%s, %s, %s)"
                    params += last
                statement += " ORDER BY principal_type, principal_id, member_user_id LIMIT %s"
                rows = await self._page(connection, statement, params + (self._page_size,))
                for row in rows:
                    yield row
                if len(rows) < self._page_size:
                    return
                last = tuple(rows[-1])

    async def iter_knowledge_scopes(
        self, *, tenant_key: str, space_id: str | None = None, kb_id: str | None = None,
        max_grants_per_snapshot: int | None = None,
    ) -> AsyncIterator[ExternalKnowledgeScopeSnapshot]:
        last: tuple[object, ...] | None = None
        pending: list[tuple[Any, ...]] = []
        pending_grants = 0
        order = "snapshot.snapshot_id, COALESCE(scope_grant.subject_type, ''), COALESCE(scope_grant.subject_id, '')"
        async with self._read_connection() as connection:
            while True:
                statement = _KNOWLEDGE_SCOPE_SQL.format(source_placeholder="%s", tenant_placeholder="%s")
                if self.source == "DINGTALK":
                    statement = statement.replace("scope_grant.subject_type, scope_grant.subject_id", "scope_grant.subject_type, scope_grant.subject_id, snapshot.metadata")
                params: tuple[object, ...] = (self.source, tenant_key)
                if space_id is not None:
                    statement += " AND snapshot.space_id = %s"
                    params += (space_id,)
                if kb_id is not None:
                    statement += " AND snapshot.kb_id = %s"
                    params += (kb_id,)
                if last is not None:
                    statement += f" AND ({order}) > (%s, %s, %s)"
                    params += last
                statement += f" ORDER BY {order} LIMIT %s"
                rows = await self._page(connection, statement, params + (self._page_size,))
                for raw_row in rows:
                    row = tuple(raw_row)
                    if self.source == "DINGTALK":
                        if len(row) != 14:
                            raise AclMembershipSnapshotError("DingTalk observation is missing")
                        try:
                            metadata = json.loads(row[13]) if isinstance(row[13], str) else row[13]
                            observed = datetime.fromisoformat(metadata["last_observed_at"])
                            if observed.tzinfo is None or not datetime.now(UTC) - timedelta(seconds=900) <= observed <= datetime.now(UTC) + timedelta(seconds=60):
                                raise ValueError("stale")
                        except (ValueError, TypeError, KeyError):
                            raise AclMembershipSnapshotError("DingTalk permissions are stale") from None
                        row = row[:13]
                    if len(row) != 13:
                        raise AclMembershipSnapshotError("knowledge scope snapshot row is invalid")
                    if pending and pending[0][0] != row[0]:
                        yield _parse_knowledge_scopes(pending)[0]
                        pending = []
                        pending_grants = 0
                    if row[11] is not None:
                        pending_grants += 1
                        if max_grants_per_snapshot is not None and pending_grants > max_grants_per_snapshot:
                            raise AclMembershipSnapshotCapacityError("knowledge scope grant limit exceeded")
                    pending.append(row)
                if len(rows) < self._page_size:
                    if pending:
                        yield _parse_knowledge_scopes(pending)[0]
                    return
                last_row = tuple(rows[-1])
                last = (last_row[0], last_row[11] or "", last_row[12] or "")


def _parse_knowledge_scopes(scope_rows: Any) -> tuple[ExternalKnowledgeScopeSnapshot, ...]:
    snapshot_data: dict[str, tuple[tuple[Any, ...], set[ExternalKnowledgeScopeGrant]]] = {}
    for raw_row in scope_rows:
        row = tuple(raw_row)
        if len(row) != 13:
            raise AclMembershipSnapshotError("knowledge scope snapshot row is invalid")
        string_values = row[:10]
        if not all(isinstance(value, str) for value in string_values):
            raise AclMembershipSnapshotError("knowledge scope snapshot row is invalid")
        (
            snapshot_id,
            tenant_key,
            source_scope_type,
            source_scope_id,
            source_scope_name,
            space_id,
            kb_id,
            generation,
            fingerprint,
            status,
        ) = (value.strip() for value in string_values)
        source_updated_at = row[10]
        if source_updated_at is not None and not isinstance(source_updated_at, datetime):
            raise AclMembershipSnapshotError("knowledge scope snapshot timestamp is invalid")
        if (
            not all(
                (
                    snapshot_id,
                    tenant_key,
                    source_scope_type,
                    source_scope_id,
                    space_id,
                    kb_id,
                    generation,
                    fingerprint,
                )
            )
            or status not in _SCOPE_STATUSES
        ):
            raise AclMembershipSnapshotError("knowledge scope snapshot row is incomplete")
        metadata = (
            snapshot_id,
            tenant_key,
            source_scope_type,
            source_scope_id,
            source_scope_name,
            space_id,
            kb_id,
            generation,
            fingerprint,
            status,
            source_updated_at,
        )
        existing = snapshot_data.get(snapshot_id)
        if existing is None:
            grants: set[ExternalKnowledgeScopeGrant] = set()
            snapshot_data[snapshot_id] = (metadata, grants)
        elif existing[0] != metadata:
            raise AclMembershipSnapshotError("knowledge scope snapshot rows are inconsistent")
        else:
            grants = existing[1]
        subject_type, subject_id = row[11:]
        if subject_type is None and subject_id is None:
            continue
        if not isinstance(subject_type, str) or not isinstance(subject_id, str):
            raise AclMembershipSnapshotError("knowledge scope grant row is invalid")
        subject_type = subject_type.strip()
        subject_id = subject_id.strip()
        if not subject_id or subject_type not in _SCOPE_SUBJECT_TYPES:
            raise AclMembershipSnapshotError("knowledge scope grant row is incomplete")
        grants.add(ExternalKnowledgeScopeGrant(subject_type=subject_type, subject_id=subject_id))

    snapshots = [
        ExternalKnowledgeScopeSnapshot(
            snapshot_id=metadata[0],
            tenant_key=metadata[1],
            source_scope_type=metadata[2],
            source_scope_id=metadata[3],
            source_scope_name=metadata[4],
            space_id=metadata[5],
            kb_id=metadata[6],
            generation=metadata[7],
            fingerprint=metadata[8],
            status=metadata[9],
            source_updated_at=metadata[10],
            grants=tuple(sorted(grants, key=lambda item: (item.subject_type, item.subject_id))),
        )
        for metadata, grants in snapshot_data.values()
    ]
    return tuple(sorted(snapshots, key=lambda item: item.snapshot_id))


class DingTalkAclMembershipSnapshotClient(FeishuAclMembershipSnapshotClient):
    source = "DINGTALK"


def acl_snapshot_client_for(provider):
    value = getattr(provider, "value", provider)
    if value == "feishu":
        return FeishuAclMembershipSnapshotClient
    if value == "dingtalk":
        return DingTalkAclMembershipSnapshotClient
    raise ValueError("ETL permission source is unsupported")
