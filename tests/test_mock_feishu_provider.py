from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.dev.mock_feishu_provider import APP_ID, APP_SECRET, TENANT_KEY, create_app
from scripts.dev.run_feishu_mock_console import loopback_only
from server.config import reset_config
from server.core.agent_access import has_agent_access
from server.core.crypto import encrypt
from server.enterprise_identity.feishu import FeishuDirectoryClient, FeishuDirectoryError
from server.enterprise_identity.service import sync_identity_source
from server.models import (
    Agent,
    AgentGroupAssignment,
    Base,
    EnterpriseDirectoryMembership,
    EnterpriseIdentitySource,
    IdentitySourceProvider,
    User,
    UserExternalIdentity,
    UserStatus,
)


def directory_client(app, **kwargs):
    return FeishuDirectoryClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        base_url="http://feishu.test",
        transport=httpx.ASGITransport(app=app),
        **kwargs,
    )


@pytest.mark.parametrize(
    "event,args",
    [
        ("socket.getaddrinfo", ("open.feishu.cn",)),
        ("socket.connect", (object(), ("192.0.2.1", 443))),
        ("socket.sendto", (object(), b"data", ("10.0.0.1", 443))),
    ],
)
def test_console_denies_external_dns_and_network(event, args):
    with pytest.raises(ConnectionRefusedError, match="denies non-loopback"):
        loopback_only(event, args)
    loopback_only("socket.connect", (object(), ("127.0.0.1", 18765)))


async def test_official_directory_shapes_paginate_and_keep_user_ids():
    app = create_app()
    async with directory_client(app) as client:
        snapshot = await client.fetch_snapshot()
        assert (await client.fetch_user("mock-alice"))["id"] == "mock-alice"
        assert (await client.fetch_department("od-mock-child"))["parent_id"] == "od-mock-research"
    assert {user["id"] for user in snapshot.users} == {"mock-alice", "mock-bob"}
    assert {group["id"] for group in snapshot.groups} == {
        "od-mock-research",
        "od-mock-child",
        "mock-reviewers",
    }
    assert {(item["group_id"], item["member_type"], item["member_id"]) for item in snapshot.memberships} >= {
        ("od-mock-research", "group", "od-mock-child"),
        ("od-mock-child", "user", "mock-bob"),
        ("mock-reviewers", "user", "mock-bob"),
    }
    requests = app.state.feishu_mock.requests
    assert any(item["query"].get("page_token") == "mock-page-1" for item in requests)
    assert all("Authorization" not in json.dumps(item) and APP_SECRET not in json.dumps(item) for item in requests)


@pytest.mark.parametrize("scenario", ["second_page_failure", "missing_page_token", "permission_denied"])
async def test_incomplete_or_denied_directory_is_never_returned_as_a_snapshot(scenario):
    async with directory_client(create_app(scenario=scenario)) as client:
        with pytest.raises((FeishuDirectoryError, httpx.HTTPStatusError)):
            await client.fetch_snapshot()


async def test_mock_rejects_wrong_credentials_ids_and_unknown_scenarios():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://feishu.test"
    ) as client:
        bad = await client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": APP_ID,
                "app_secret": "wrong",
            },
        )
        assert bad.json()["code"] == 10015
        assert (await client.get("/open-apis/contact/v3/users")).json()["code"] == 99991663
        token = await client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": APP_ID,
                "app_secret": APP_SECRET,
            },
        )
        result = await client.get(
            "/open-apis/contact/v3/users",
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token.json()['tenant_access_token']}"},
        )
        assert result.status_code == 400
        assert (await client.post("/__mock/scenario", json={"scenario": "typo"})).status_code == 422
        assert (await client.get("/unimplemented-api")).status_code == 404


async def test_browser_codes_bind_redirect_and_are_single_use():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://feishu.test"
    ) as client:
        redirect = "http://127.0.0.1:18760/auth/feishu/tenant-verification/callback"
        request = {"client_id": APP_ID, "redirect_uri": redirect, "state": "browser-state"}
        response = await client.get("/open-apis/authen/v1/authorize", params=request)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["state"] == ["browser-state"]
        body = {
            "grant_type": "authorization_code",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "code": query["code"][0],
            "redirect_uri": redirect,
        }
        wrong = await client.post(
            "/open-apis/authen/v2/oauth/token", json={**body, "redirect_uri": redirect + "/wrong"}
        )
        assert wrong.status_code == 400
        token = await client.post("/open-apis/authen/v2/oauth/token", json=body)
        assert token.status_code == 200
        assert (await client.post("/open-apis/authen/v2/oauth/token", json=body)).status_code == 400
        info = await client.get(
            "/open-apis/authen/v1/user_info",
            headers={
                "Authorization": f"Bearer {token.json()['access_token']}",
            },
        )
        assert info.json()["data"]["tenant_key"] == TENANT_KEY
        assert info.json()["data"]["user_id"] == "mock-alice"
        forbidden = await client.get(
            "/open-apis/authen/v1/authorize",
            params={
                **request,
                "redirect_uri": "https://example.com/auth/feishu/login/callback",
            },
        )
        assert forbidden.status_code == 400


@pytest.fixture
async def database(monkeypatch):
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"m" * 32).decode())
    reset_config()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()
        reset_config()


async def test_real_sync_removes_departed_group_access_and_preserves_failed_snapshot(database):
    session = database
    app = create_app()
    source = EnterpriseIdentitySource.create(
        name="Mock Feishu", provider=IdentitySourceProvider.FEISHU, tenant_id=TENANT_KEY
    )
    source.config_ciphertext = encrypt(json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}))
    agent = Agent(name="Mock group access")
    session.add_all([source, agent])
    await session.flush()
    agent_id = agent.id
    session.add(
        AgentGroupAssignment.for_identity_source_group(
            agent_id=agent.id,
            identity_source_id=source.id,
            external_group_id="mock-reviewers",
            created_by_user_id=None,
        )
    )
    await session.commit()

    async def synchronize():
        await sync_identity_source(session, source, feishu_client_factory=lambda **_: directory_client(app))
        await session.commit()

    await synchronize()
    bob_id = await session.scalar(
        select(UserExternalIdentity.user_id).where(
            UserExternalIdentity.identity_provider == f"feishu:{TENANT_KEY}",
            UserExternalIdentity.external_subject == "mock-bob",
        )
    )
    assert bob_id and await has_agent_access(session, agent_id, bob_id)
    old_members = list((await session.execute(select(EnterpriseDirectoryMembership.id))).scalars())
    old_synced = source.last_synced_at
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://feishu.test") as control:
        await control.post("/__mock/scenario", json={"scenario": "second_page_failure"})
        with pytest.raises(httpx.HTTPStatusError):
            await synchronize()
        await session.rollback()
        await session.refresh(source)
        assert source.last_synced_at.replace(tzinfo=None) == old_synced.replace(tzinfo=None)
        assert list((await session.execute(select(EnterpriseDirectoryMembership.id))).scalars()) == old_members
        await control.post("/__mock/scenario", json={"scenario": "removed_user"})
        await synchronize()
    assert not await has_agent_access(session, agent_id, bob_id)
    bob = await session.get(User, bob_id, populate_existing=True)
    assert bob.status == UserStatus.DISABLED
