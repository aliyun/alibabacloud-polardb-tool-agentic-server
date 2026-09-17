from __future__ import annotations

from server.auth.oauth_provider import PASAuthProvider
from server.auth.personal_access import request_mcp_mode
from server.config import get_config
from server.core.personal_token_service import CLIENT_ID, TOKEN_PREFIX

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_personal_token_owner_revocation_and_endpoint_separation(client, setup):
    http, admin_headers, member_headers = client
    factory, admin, member = setup
    response = await http.post("/api/me/personal-tokens", headers=member_headers, json={})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token"].startswith(TOKEN_PREFIX)
    listing = await http.get("/api/me/personal-tokens", headers=member_headers)
    assert listing.status_code == 200
    assert "token" not in listing.json()["items"][0]
    assert listing.json()["mcp_url"].endswith("/mcp/personal")
    assert (await http.get("/api/me/personal-tokens", headers=admin_headers)).json()["items"] == []
    assert (await http.delete(f"/api/me/personal-tokens/{body['id']}", headers=admin_headers)).status_code == 404
    assert (await http.post("/api/me/personal-tokens", headers=member_headers, json={})).status_code == 409
    assert (await http.get("/api/users", headers={"Authorization": f"Bearer {body['token']}"})).status_code == 401
    provider = PASAuthProvider(session_factory=factory, config=get_config())
    marker = request_mcp_mode.set("personal")
    try:
        token = await provider.load_access_token(body["token"])
        assert token.subject == f"user:{member.id}"
        assert token.client_id == CLIENT_ID
        assert token.claims["access_mode"] == "personal"
        request_mcp_mode.set("legacy")
        assert await provider.load_access_token(body["token"]) is None
    finally:
        request_mcp_mode.reset(marker)
    assert await provider.load_refresh_token(None, body["token"]) is None
    assert (await http.delete(f"/api/me/personal-tokens/{body['id']}", headers=member_headers)).status_code == 204
    assert await provider.load_access_token(body["token"]) is None


async def test_personal_token_api_requires_authentication_and_valid_expiry(client):
    http, _, member_headers = client
    assert (await http.get("/api/me/personal-tokens")).status_code == 401
    for days in [0, 366]:
        response = await http.post("/api/me/personal-tokens", headers=member_headers, json={"expires_in_days": days})
        assert response.status_code == 422


async def test_token_audit_failure_does_not_issue_credential(client, setup, monkeypatch):
    from server.core.personal_token_service import list_tokens
    http, _, member_headers = client
    factory, _, member = setup
    async def fail(*args, **kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr('server.api.personal_tokens.log_audit', fail)
    response = await http.post('/api/me/personal-tokens', headers=member_headers, json={})
    assert response.status_code == 503
    async with factory() as session:
        assert await list_tokens(session, member.id) == []
