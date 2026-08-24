from __future__ import annotations

import json

import httpx
import pytest

from server.enterprise_identity.feishu import FeishuDirectoryClient


@pytest.mark.asyncio
async def test_feishu_directory_client_uses_tenant_user_ids_and_source_principal_types():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert json.loads(request.content) == {"app_id": "cli_test", "app_secret": "secret"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        assert request.headers["Authorization"] == "Bearer tenant-token"
        if request.url.path == "/open-apis/contact/v3/users":
            assert request.url.params["user_id_type"] == "user_id"
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "user_id": "u_design",
                            "name": "Design user",
                            "email": "design@example.com",
                        },
                    ],
                    "has_more": False,
                },
            })
        if request.url.path == "/open-apis/contact/v3/departments/0/children":
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [{"open_department_id": "od_research", "name": "Research"}],
                    "has_more": False,
                },
            })
        if request.url.path == "/open-apis/contact/v3/departments/od_research/children":
            return httpx.Response(200, json={
                "code": 0,
                "data": {"items": [], "has_more": False},
            })
        if request.url.path == "/open-apis/contact/v3/users/find_by_department":
            assert request.url.params["user_id_type"] == "user_id"
            if request.url.params["department_id"] == "0":
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {
                        "items": [{"user_id": "u_root", "name": "Root"}],
                        "has_more": False,
                    },
                })
            assert request.url.params["department_id"] == "od_research"
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [{"user_id": "u_design", "name": "Design user"}],
                    "has_more": False,
                },
            })
        if request.url.path == "/open-apis/contact/v3/group/simplelist":
            return httpx.Response(200, json={
                "code": 0,
                "data": {"grouplist": [{"group_id": "g_engineering", "name": "Engineering"}], "has_more": False},
            })
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            assert request.url.params["member_id_type"] == "user_id"
            user_id = request.url.params["member_id"]
            groups = ["g_engineering"] if user_id == "u_design" else []
            return httpx.Response(200, json={"code": 0, "data": {"group_list": groups, "has_more": False}})
        raise AssertionError(f"unexpected request: {request.url}")

    async with FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        snapshot = await client.fetch_snapshot()

    assert snapshot.users == [
        {"id": "u_design", "display_name": "Design user", "email": "design@example.com"},
        {"id": "u_root", "display_name": "Root", "email": None},
    ]
    assert snapshot.groups == [
        {"id": "g_engineering", "display_name": "Engineering", "principal_type": "group"},
        {"id": "od_research", "display_name": "Research", "principal_type": "department"},
    ]
    assert snapshot.memberships == [
        {"group_id": "g_engineering", "member_type": "user", "member_id": "u_design"},
        {"group_id": "od_research", "member_type": "user", "member_id": "u_design"},
    ]
    assert any(
        request.url.path.endswith("/users/find_by_department")
        and request.url.params["department_id"] == "0"
        for request in requests
    )
    assert any(request.url.path.endswith("/member_belong") for request in requests)
    assert any("department" in request.url.path for request in requests)
