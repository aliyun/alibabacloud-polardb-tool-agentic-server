from __future__ import annotations

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

    async def execute(self, statement: str, params: tuple[object, ...]) -> None:
        self.statement = statement
        self.params = params

    async def fetchall(self):
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
            "autocommit": True,
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

    assert connection.cursor_instance.params == ("FEISHU", "tenant-a")
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
