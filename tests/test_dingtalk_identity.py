from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from server.enterprise_identity.dingtalk import DingTalkDirectoryClient, DingTalkIdentityError
from server.enterprise_identity.dingtalk_auth import build_dingtalk_authorization_url, authenticate_dingtalk_user


def responder(request):
    path = request.url.path
    if path.endswith("/userAccessToken"):
        return httpx.Response(200, json={"accessToken": "user-token", "corpId": "corp-a"})
    if path.endswith("/accessToken"):
        return httpx.Response(200, json={"accessToken": "app-token", "expireIn": 7200})
    if path.endswith("/users/me"):
        assert request.headers["x-acs-dingtalk-access-token"] == "user-token"
        return httpx.Response(200, json={"unionId": "union-a", "nick": "Member"})
    if path.endswith("/getbyunionid"):
        assert request.url.params["access_token"] == "app-token"
        return httpx.Response(200, json={"errcode": 0, "result": {"userid": "user-a", "contact_type": 0}})
    if path.endswith("/v2/user/get"):
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {
                    "userid": "user-a",
                    "unionid": "union-a",
                    "name": "Member",
                    "active": True,
                    "dept_id_list": [2],
                },
            },
        )
    if path == "/auth/scopes":
        return httpx.Response(200, json={"errcode": 0, "auth_org_scopes": {"authed_dept": [2], "authed_user": []}})
    if path.endswith("/department/get"):
        return httpx.Response(200, json={"errcode": 0, "result": {"dept_id": 2, "name": "Engineering"}})
    if path.endswith("/department/listsub"):
        return httpx.Response(200, json={"errcode": 0, "result": []})
    if path.endswith("/v2/user/list"):
        import json
        assert json.loads(request.content)["size"] == 100
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {"list": [{"userid": "user-a", "name": "Member", "active": True}], "has_more": False},
            },
        )
    raise AssertionError(path)


@pytest.mark.asyncio
async def test_login_proves_tenant_and_internal_user_id():
    result = await authenticate_dingtalk_user(
        client_id="app",
        client_secret="secret",
        tenant_id="corp-a",
        code="code",
        transport=httpx.MockTransport(responder),
    )
    assert result["user_id"] == "user-a"
    assert result["tenant_key"] == "corp-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["tenant", "external", "union", "inactive"])
async def test_login_rejects_untrusted_identity(case):
    def handler(r):
        response = responder(r)
        payload = response.json()
        if case == "tenant" and r.url.path.endswith("/userAccessToken"):
            payload["corpId"] = "corp-other"
        if case == "external" and r.url.path.endswith("/getbyunionid"):
            payload["result"]["contact_type"] = 1
        if case == "union" and r.url.path.endswith("/v2/user/get"):
            payload["result"]["unionid"] = "another"
        if case == "inactive" and r.url.path.endswith("/v2/user/get"):
            payload["result"]["active"] = False
        return httpx.Response(200, json=payload)

    with pytest.raises(DingTalkIdentityError):
        await authenticate_dingtalk_user(
            client_id="app",
            client_secret="secret",
            tenant_id="corp-a",
            code="code",
            transport=httpx.MockTransport(handler),
        )


def test_authorization_requests_selected_corporation_and_state():
    url = build_dingtalk_authorization_url(
        client_id="app",
        tenant_id="corp-a",
        redirect_uri="https://pas.example/auth/dingtalk/login/callback",
        state="state",
    )
    q = parse_qs(urlsplit(url).query)
    assert q["scope"] == ["openid corpid"] and q["corpId"] == ["corp-a"]
    assert q["state"] == ["state"] and q["prompt"] == ["consent"]


@pytest.mark.asyncio
async def test_directory_has_native_departments_and_deduplicated_members():
    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(responder)
    ) as client:
        snapshot = await client.fetch_snapshot()
    assert [u["id"] for u in snapshot.users] == ["user-a"]
    assert snapshot.groups[0]["principal_type"] == "department"
    assert snapshot.groups[0]["display_name"] == "Engineering"
    assert snapshot.memberships == [{"group_id": "2", "member_id": "user-a", "member_type": "user"}]


@pytest.mark.asyncio
async def test_repeated_cursor_fails_without_publishing_partial_directory():
    def handler(r):
        if r.url.path.endswith("/v2/user/list"):
            return httpx.Response(200, json={"errcode": 0, "result": {"list": [], "has_more": True, "next_cursor": 0}})
        return responder(r)

    async with DingTalkDirectoryClient(
        tenant_id="corp-a", client_id="app", client_secret="secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(DingTalkIdentityError):
            await client.fetch_snapshot()
