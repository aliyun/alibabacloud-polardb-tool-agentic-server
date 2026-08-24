from __future__ import annotations

import httpx
import pytest

from server.enterprise_identity.sharepoint import SharePointDirectoryClient


@pytest.mark.asyncio
async def test_sharepoint_directory_client_builds_snapshot_from_graph_pages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/tenant-001/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "graph-token"})
        if request.url.path == "/v1.0/users" and request.url.params.get("$skiptoken") == "next":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "user-2",
                            "displayName": "Bob",
                            "userPrincipalName": "bob@example.test",
                        }
                    ]
                },
            )
        if request.url.path == "/v1.0/users":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "user-1",
                            "displayName": "Alice",
                            "mail": "alice@example.test",
                        },
                        {
                            "id": "user-disabled",
                            "displayName": "Disabled user",
                            "accountEnabled": False,
                        },
                    ],
                    "@odata.nextLink": "https://graph.microsoft.test/v1.0/users?$skiptoken=next",
                },
            )
        if request.url.path == "/v1.0/groups":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "group-1", "displayName": "Engineering"},
                        {"id": "group-2", "displayName": "Product"},
                    ]
                },
            )
        if request.url.path == "/v1.0/groups/group-1/members":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"@odata.type": "#microsoft.graph.user", "id": "user-1"},
                        {"@odata.type": "#microsoft.graph.group", "id": "group-2"},
                        {"@odata.type": "#microsoft.graph.servicePrincipal", "id": "app-1"},
                    ]
                },
            )
        if request.url.path == "/v1.0/groups/group-2/members":
            return httpx.Response(200, json={"value": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with SharePointDirectoryClient(
        tenant_id="tenant-001",
        client_id="client-id",
        client_secret="client-secret",
        authority_url="https://login.microsoft.test",
        graph_url="https://graph.microsoft.test/v1.0",
        transport=transport,
    ) as client:
        snapshot = await client.fetch_snapshot()

    assert snapshot.users == [
        {"id": "user-1", "display_name": "Alice", "email": "alice@example.test"},
        {"id": "user-2", "display_name": "Bob", "email": "bob@example.test"},
    ]
    assert snapshot.groups == [
        {"id": "group-1", "display_name": "Engineering"},
        {"id": "group-2", "display_name": "Product"},
    ]
    assert snapshot.memberships == [
        {"group_id": "group-1", "member_type": "group", "member_id": "group-2"},
        {"group_id": "group-1", "member_type": "user", "member_id": "user-1"},
    ]
    assert requests[0].url.path == "/tenant-001/oauth2/v2.0/token"
    assert requests[0].content


@pytest.mark.asyncio
async def test_sharepoint_directory_client_uses_china_cloud_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.partner.microsoftonline.cn":
            return httpx.Response(200, json={"access_token": "graph-token"})
        return httpx.Response(200, json={"value": []})

    async with SharePointDirectoryClient(
        tenant_id="tenant-001",
        client_id="client-id",
        client_secret="client-secret",
        cloud="china",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.fetch_snapshot()

    assert requests[0].url == "https://login.partner.microsoftonline.cn/tenant-001/oauth2/v2.0/token"
    assert b"scope=https%3A%2F%2Fmicrosoftgraph.chinacloudapi.cn%2F.default" in requests[0].content
    assert any(request.url.host == "microsoftgraph.chinacloudapi.cn" for request in requests[1:])
