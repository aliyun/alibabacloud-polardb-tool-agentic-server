import json
import pytest
from sqlalchemy import select
from server.core.crypto import decrypt, encrypt
from server.models import (
    EnterpriseIdentitySource,
    IdentitySourceProvider,
    EnterpriseIdentitySourceStatus,
    DingTalkLoginState,
    User,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture(autouse=True)
def configured_public_url(clean, monkeypatch):
    from server.config import get_config

    monkeypatch.setattr(get_config().server, "public_base_url", "https://pas.example.test")


async def source(setup):
    async with setup[0]() as session:
        s = EnterpriseIdentitySource.create(
            name="DingTalk", provider=IdentitySourceProvider.DINGTALK, tenant_id="corp-a"
        )
        s.status = EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
        s.config_ciphertext = encrypt(json.dumps({"client_id": "app", "client_secret": "secret"}))
        session.add(s)
        await session.commit()
        return s.id


async def test_admin_verification_binds_browser_and_consumes_state_once(client, setup, monkeypatch):
    from server.enterprise_identity import dingtalk_auth

    sid = await source(setup)
    http, admin, member = client
    assert (await http.post(f"/api/identity-sources/{sid}/dingtalk-verification", headers=member)).status_code == 403
    start = await http.post(f"/api/identity-sources/{sid}/dingtalk-verification", headers=admin)
    assert start.status_code == 200 and "secret" not in start.text
    async with setup[0]() as session:
        row = await session.scalar(select(DingTalkLoginState))
        assert row.created_by_user_id == setup[1].id
        assert row.purpose == "verify"
    from urllib.parse import urlsplit, parse_qs

    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    cookie = http.cookies.get("dingtalk_state")
    http.cookies.clear()
    # Another browser cannot consume the initiator's state.
    assert (await http.get("/auth/dingtalk/callback", params={"code": "c", "state": state})).status_code == 400
    http.cookies.set("dingtalk_state", cookie)

    async def auth(**kwargs):
        return {"tenant_key": "corp-a", "user_id": "member-a", "display_name": "Member", "email": None}

    monkeypatch.setattr(dingtalk_auth, "authenticate_dingtalk_user", auth)
    complete = await http.get("/auth/dingtalk/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert complete.status_code == 303
    async with setup[0]() as session:
        s = await session.get(EnterpriseIdentitySource, sid)
        assert s.status == EnterpriseIdentitySourceStatus.PENDING_BINDING
        assert await session.scalar(select(DingTalkLoginState)) is None
    assert (await http.get("/auth/dingtalk/callback", params={"code": "c", "state": state})).status_code == 400


async def test_rotating_credentials_invalidates_pending_verification(client, setup, monkeypatch):
    sid = await source(setup)
    http, admin, _ = client
    start = await http.post(f"/api/identity-sources/{sid}/dingtalk-verification", headers=admin)
    from urllib.parse import urlsplit, parse_qs

    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    async with setup[0]() as session:
        s = await session.get(EnterpriseIdentitySource, sid)
        s.config_ciphertext = encrypt(json.dumps({"client_id": "app", "client_secret": "rotated"}))
        await session.commit()
    response = await http.get("/auth/dingtalk/callback", params={"code": "c", "state": state})
    assert response.status_code == 400
    async with setup[0]() as session:
        assert (
            await session.get(EnterpriseIdentitySource, sid)
        ).status == EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION


async def test_unverified_source_explains_admin_verification_without_starting_oauth(client, setup):
    await source(setup)
    http, _, _ = client
    response = await http.get('/auth/dingtalk/login', follow_redirects=False)
    assert response.status_code == 409
    assert 'Verify DingTalk enterprise' in response.text
    assert '同步目录' in response.text
    assert 'secret' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    async with setup[0]() as session:
        assert await session.scalar(select(DingTalkLoginState)) is None


async def test_dingtalk_login_restores_session_for_linked_builtin_account(client, setup, monkeypatch):
    from datetime import datetime, timezone
    from urllib.parse import urlsplit, parse_qs
    from server.enterprise_identity import dingtalk_auth
    from server.enterprise_identity.service import upsert_external_user
    from server.auth.jwt_manager import verify_token

    sid = await source(setup)
    async with setup[0]() as session:
        s = await session.get(EnterpriseIdentitySource, sid)
        s.status = EnterpriseIdentitySourceStatus.ACTIVE
        s.last_synced_at = datetime.now(timezone.utc)
        config = json.loads(decrypt(s.config_ciphertext))
        config['tenant_verified_digest'] = dingtalk_auth.dingtalk_config_digest(s, config)
        s.config_ciphertext = encrypt(json.dumps(config))
        await upsert_external_user(session, s, external_user_id='member-a', display_name='Member',
                                   email=None, user=await session.get(User, setup[2].id))
        await session.commit()

    async def auth(**kwargs):
        return {'tenant_key': 'corp-a', 'user_id': 'member-a', 'display_name': 'Member', 'email': None}

    monkeypatch.setattr(dingtalk_auth, 'authenticate_dingtalk_user', auth)
    http, _, _ = client
    start = await http.get('/auth/dingtalk/login', follow_redirects=False)
    assert start.status_code == 303
    state = parse_qs(urlsplit(start.headers['location']).query)['state'][0]
    callback = await http.get('/auth/dingtalk/callback', params={'code': 'c', 'state': state}, follow_redirects=False)
    assert callback.status_code == 303
    token = callback.cookies['session_token']
    assert verify_token(token)['credential_epoch'] == setup[2].credential_epoch
    me = await http.get('/auth/me')
    assert me.status_code == 200
    assert me.json()['id'] == setup[2].id


async def test_dingtalk_manual_directory_sync_reaches_registered_adapter(client, setup, monkeypatch):
    from server.enterprise_identity.dingtalk_auth import dingtalk_config_digest
    import asyncio

    sid = await source(setup)
    async with setup[0]() as session:
        s = await session.get(EnterpriseIdentitySource, sid)
        config = json.loads(decrypt(s.config_ciphertext))
        config['tenant_verified_digest'] = dingtalk_config_digest(s, config)
        s.config_ciphertext = encrypt(json.dumps(config))
        s.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
        await session.commit()
    called = asyncio.Event()

    async def sync(_session, s):
        assert s.id == sid and s.provider == IdentitySourceProvider.DINGTALK
        s.status = EnterpriseIdentitySourceStatus.ACTIVE
        called.set()

    monkeypatch.setattr('server.api.identity_sources.sync_identity_source', sync)
    http, admin, member = client
    assert (await http.post(f'/api/identity-sources/{sid}/sync', headers=member)).status_code == 403
    result = await http.post(f'/api/identity-sources/{sid}/sync', headers=admin)
    assert result.status_code in (200, 202)
    await asyncio.wait_for(called.wait(), timeout=2)


async def test_unverified_dingtalk_sync_preserves_verification_state(client, setup):
    sid = await source(setup)
    http, admin, _ = client
    result = await http.post(f'/api/identity-sources/{sid}/sync', headers=admin)
    assert result.status_code == 409
    async with setup[0]() as session:
        s = await session.get(EnterpriseIdentitySource, sid)
        assert s.status == EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
        assert s.sync_worker_id is None


async def test_credential_update_retires_old_pending_directory_events(client, setup):
    from server.models import EnterpriseIdentitySourceEvent, EnterpriseIdentitySourceEventStatus
    http, admin_headers, _ = client
    source_id = await source(setup)
    async with setup[0]() as session:
        event = EnterpriseIdentitySourceEvent(identity_source_id=source_id, event_type='user_modify_org', subject_id='old-user')
        session.add(event)
        await session.commit()
        event_id = event.id
    response = await http.put('/api/identity-sources/' + source_id, headers=admin_headers,
                              json={'name': 'DingTalk', 'client_id': 'new-app', 'client_secret': 'new-secret'})
    assert response.status_code == 200
    async with setup[0]() as session:
        event = await session.get(EnterpriseIdentitySourceEvent, event_id)
        assert event.status == EnterpriseIdentitySourceEventStatus.PROCESSED
        current = await session.get(EnterpriseIdentitySource, source_id)
        assert current.status == EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION


@pytest.mark.parametrize("public_url", [
    "", " ", "/pas", "//untrusted.example", "ftp://pas.example.test",
    "https://user:password@pas.example.test", "https://pas.example.test?token=value",
    "https://pas.example.test#fragment", "https://pas.example.test:invalid",
])
async def test_verification_rejects_missing_or_invalid_public_url_without_oauth_state(client, setup, public_url):
    from server.config import get_config

    sid = await source(setup)
    get_config().server.public_base_url = public_url
    http, admin, _ = client
    result = await http.post(f"/api/identity-sources/{sid}/dingtalk-verification", headers={
        **admin, "X-Forwarded-Host": "untrusted.example", "X-Forwarded-Proto": "https",
    })
    assert result.status_code == 409
    assert "runtime_policy.external_base_url" in result.json()["detail"]
    assert "authorization_url" not in result.json()
    assert "dingtalk_state" not in result.cookies
    assert result.headers["cache-control"] == "no-store"
    async with setup[0]() as session:
        assert await session.scalar(select(DingTalkLoginState)) is None
        assert (await session.get(EnterpriseIdentitySource, sid)).status == EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION


async def test_verified_login_explains_missing_public_url_without_redirect_or_state(client, setup):
    from datetime import datetime, timezone
    from server.config import get_config
    from server.enterprise_identity.dingtalk_auth import dingtalk_config_digest

    sid = await source(setup)
    async with setup[0]() as session:
        current = await session.get(EnterpriseIdentitySource, sid)
        config = json.loads(decrypt(current.config_ciphertext))
        config["tenant_verified_digest"] = dingtalk_config_digest(current, config)
        current.config_ciphertext = encrypt(json.dumps(config))
        current.status = EnterpriseIdentitySourceStatus.ACTIVE
        current.last_synced_at = datetime.now(timezone.utc)
        await session.commit()
    get_config().server.public_base_url = ""
    http, _, _ = client
    result = await http.get("/auth/dingtalk/login", follow_redirects=False)
    assert result.status_code == 409
    assert "runtime_policy.external_base_url" in result.text
    assert "外部访问" in result.text
    assert "secret" not in result.text
    assert "location" not in result.headers
    assert "dingtalk_state" not in result.cookies
    assert result.headers["cache-control"] == "no-store"
    async with setup[0]() as session:
        assert await session.scalar(select(DingTalkLoginState)) is None


@pytest.mark.parametrize("public_url", ["https://pas.example.test", "https://pas.example.test/", "http://127.0.0.1:18760"])
async def test_verification_uses_configured_absolute_callback_after_configuration_repair(client, setup, public_url):
    from urllib.parse import parse_qs, urlsplit
    from server.config import get_config

    sid = await source(setup)
    http, admin, _ = client
    get_config().server.public_base_url = ""
    rejected = await http.post(f"/api/identity-sources/{sid}/dingtalk-verification", headers=admin)
    assert rejected.status_code == 409
    get_config().server.public_base_url = public_url
    result = await http.post(f"/api/identity-sources/{sid}/dingtalk-verification", headers=admin)
    assert result.status_code == 200
    query = parse_qs(urlsplit(result.json()["authorization_url"]).query)
    assert query["redirect_uri"] == [public_url.rstrip("/") + "/auth/dingtalk/callback"]
    assert "dingtalk_state" in result.cookies
    async with setup[0]() as session:
        assert len((await session.scalars(select(DingTalkLoginState))).all()) == 1
