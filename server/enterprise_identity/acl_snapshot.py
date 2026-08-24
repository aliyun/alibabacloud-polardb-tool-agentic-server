from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import asyncmy  # type: ignore[import-untyped]


class AclMembershipSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class AclMembershipSnapshot:
    groups: list[dict[str, str]]
    memberships: list[dict[str, str]]


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


class FeishuAclMembershipSnapshotClient:
    """Read ETL's authoritative Feishu ACL membership projection."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        database: str = "polar_rag_meta",
        connect: Callable[..., Awaitable[Any]] = asyncmy.connect,
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
        self._connect = connect

    async def fetch_snapshot(self, *, tenant_id: str) -> AclMembershipSnapshot:
        tenant_key = tenant_id.strip()
        if not tenant_key:
            raise AclMembershipSnapshotError("Feishu tenant key is required")
        connection = None
        try:
            connection = await self._connect(
                host=self._host,
                port=self._port,
                user=self._username,
                password=self._password,
                db=self._database,
                connect_timeout=10,
                autocommit=True,
            )
            async with connection.cursor() as cursor:
                await cursor.execute(_MEMBERSHIP_SQL, ("FEISHU", tenant_key))
                rows = await cursor.fetchall()
        except AclMembershipSnapshotError:
            raise
        except Exception as exc:
            raise AclMembershipSnapshotError("ACL membership snapshot query failed") from exc
        finally:
            if connection is not None:
                try:
                    await connection.ensure_closed()
                except Exception:
                    pass

        groups: dict[str, dict[str, str]] = {}
        memberships: set[tuple[str, str]] = set()
        for row in rows:
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
        )
