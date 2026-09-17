from __future__ import annotations

import json
import logging
import asyncio

import httpx
import pytest

from server.enterprise_identity.feishu import FeishuDirectoryClient


@pytest.mark.asyncio
async def test_feishu_directory_client_logs_http_status_without_credentials(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    caplog.set_level(logging.WARNING, logger="server.enterprise_identity.feishu")
    async with FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_snapshot()

    records = [
        record
        for record in caplog.records
        if record.name == "server.enterprise_identity.feishu"
    ]
    assert len(records) == 1
    record = records[0]
    assert "operation=tenant_access_token" in record.getMessage()
    assert "status=503" in record.getMessage()
    assert record.operation == "tenant_access_token"
    assert record.status_code == 503
    assert record.duration_ms >= 0
    assert "cli_test" not in record.getMessage()
    assert "secret" not in record.getMessage()


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
                "data": {
                    "items": [{"open_department_id": "od_platform", "name": "Platform"}],
                    "has_more": False,
                },
            })
        if request.url.path == "/open-apis/contact/v3/departments/od_platform/children":
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
            if request.url.params["department_id"] == "od_research":
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {
                        "items": [{"user_id": "u_design", "name": "Design user"}],
                        "has_more": False,
                    },
                })
            assert request.url.params["department_id"] == "od_platform"
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [{"user_id": "u_zhangsan", "name": "Zhang San"}],
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
        {"id": "u_zhangsan", "display_name": "Zhang San", "email": None},
    ]
    assert snapshot.groups == [
        {"id": "g_engineering", "display_name": "Engineering", "principal_type": "group"},
        {"id": "od_platform", "display_name": "Platform", "principal_type": "department"},
        {"id": "od_research", "display_name": "Research", "principal_type": "department"},
    ]
    assert snapshot.memberships == [
        {"group_id": "g_engineering", "member_type": "user", "member_id": "u_design"},
        {"group_id": "od_platform", "member_type": "user", "member_id": "u_zhangsan"},
        {"group_id": "od_research", "member_type": "group", "member_id": "od_platform"},
        {"group_id": "od_research", "member_type": "user", "member_id": "u_design"},
    ]
    assert any(
        request.url.path.endswith("/users/find_by_department")
        and request.url.params["department_id"] == "0"
        for request in requests
    )
    assert any(request.url.path.endswith("/member_belong") for request in requests)
    assert any("department" in request.url.path for request in requests)


@pytest.mark.asyncio
async def test_feishu_directory_client_fetches_department_work_concurrently():
    active = 0
    max_active = 0

    async def mark_work():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/contact/v3/users":
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/departments/0/children":
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [
                        {"open_department_id": f"od_{index}", "name": f"Department {index}"}
                        for index in range(3)
                    ],
                    "has_more": False,
                },
            })
        if request.url.path.startswith("/open-apis/contact/v3/users/find_by_department"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path.startswith("/open-apis/contact/v3/departments/"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/simplelist":
            return httpx.Response(200, json={"code": 0, "data": {"grouplist": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            return httpx.Response(200, json={"code": 0, "data": {"group_list": [], "has_more": False}})
        raise AssertionError(request.url)

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        await mark_work()
        return handler(request)

    async with FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="secret",
        transport=httpx.MockTransport(transport_handler),
    ) as client:
        await client.fetch_snapshot()

    assert max_active >= 2


@pytest.mark.asyncio
async def test_feishu_directory_client_skips_a_user_with_unavailable_group_memberships(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/contact/v3/users":
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/users/find_by_department":
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [
                        {"user_id": "u-ok", "name": "Usable"},
                        {"user_id": "u-missing", "name": "Unavailable"},
                    ],
                    "has_more": False,
                },
            })
        if request.url.path.startswith("/open-apis/contact/v3/departments/"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/simplelist":
            return httpx.Response(200, json={"code": 0, "data": {"grouplist": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            if request.url.params["member_id"] == "u-missing":
                return httpx.Response(400, json={"code": 99991672, "msg": "user unavailable"})
            return httpx.Response(200, json={"code": 0, "data": {"group_list": [], "has_more": False}})
        raise AssertionError(request.url)

    caplog.set_level(logging.WARNING, logger="server.enterprise_identity.feishu")
    async with FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        snapshot = await client.fetch_snapshot()

    assert [user["id"] for user in snapshot.users] == ["u-missing", "u-ok"]
    warning = next(
        record
        for record in caplog.records
        if record.getMessage() == "feishu.memberships_partial"
    )
    assert warning.skipped_count == 1
    assert warning.provider_errors == {"99991672": 1}


@pytest.mark.asyncio
async def test_feishu_directory_client_resumes_users_from_persistent_page_checkpoint(tmp_path):
    user_page_requests: list[str | None] = []
    pause_second_page = True

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pause_second_page
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        if request.url.path == "/open-apis/contact/v3/users":
            page_token = request.url.params.get("page_token")
            user_page_requests.append(page_token)
            if page_token is None:
                return httpx.Response(200, json={
                    "code": 0,
                    "data": {
                        "items": [{"user_id": "u_first", "name": "First"}],
                        "has_more": True,
                        "page_token": "users-page-2",
                    },
                })
            if pause_second_page:
                await asyncio.sleep(1)
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "items": [{"user_id": "u_second", "name": "Second"}],
                    "has_more": False,
                },
            })
        if request.url.path.startswith("/open-apis/contact/v3/users/find_by_department"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path.startswith("/open-apis/contact/v3/departments/"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/simplelist":
            return httpx.Response(200, json={"code": 0, "data": {"grouplist": [], "has_more": False}})
        if request.url.path == "/open-apis/contact/v3/group/member_belong":
            return httpx.Response(200, json={"code": 0, "data": {"group_list": [], "has_more": False}})
        raise AssertionError(request.url)

    async with FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
        checkpoint_dir=tmp_path,
        checkpoint_key="source-1",
    ) as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.fetch_snapshot(), timeout=0.05)

    pause_second_page = False
    async with FeishuDirectoryClient(
        app_id="cli_test",
        app_secret="secret",
        transport=httpx.MockTransport(handler),
        checkpoint_dir=tmp_path,
        checkpoint_key="source-1",
    ) as client:
        snapshot = await client.fetch_snapshot()

    assert user_page_requests == [None, "users-page-2", "users-page-2"]
    assert snapshot.users == [{"id": "u_second", "display_name": "Second", "email": None}]
