"""Integration test: full MCP authorization flow (builtin mode).

Exercises the complete OAuth 2.1 + PKCE flow end-to-end:
  1. POST /mcp (no token) -> 401 + WWW-Authenticate header
  2. GET resource_metadata URL -> Protected Resource Metadata (RFC 9728)
  3. GET /.well-known/oauth-authorization-server -> AS metadata
  4. POST /register -> Dynamic Client Registration -> client_id
  5. GET /authorize -> 302 redirect to /mcp-auth/login?session_id=...
  6. GET /mcp-auth/login?session_id=... -> HTML login form
  7. POST /mcp-auth/login/callback -> 302 redirect with code + state
  8. POST /token -> access_token (JWT) + refresh_token
  9. POST /mcp with Bearer token -> 200 (MCP response)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client
from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.sql_executor import reset_rate_limiters
from server.db import engine as engine_mod
from server.models import Base, User, AuthProvider, UserRole
from server.mcp.transport import mcp_lifespan, reset_mcp


# ---------------------------------------------------------------------------
# PKCE helper
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

REDIRECT_URI = "http://localhost:18761/callback"


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _parse_sse_response(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean():
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    reset_mcp()
    set_polardb_client(MockPolarDBClient())
    reset_rate_limiters()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()
    reset_polardb_client()
    reset_rate_limiters()


@pytest.fixture
def encryption_key():
    key = os.urandom(32)
    os.environ["PAS_ENCRYPTION_KEY"] = base64.b64encode(key).decode()
    yield key
    del os.environ["PAS_ENCRYPTION_KEY"]


@pytest.fixture
async def test_engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield e
    finally:
        await e.dispose()


@pytest.fixture
async def setup_data(test_engine, encryption_key):
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("testpass"),
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()

    return {}


@pytest.fixture
async def client(setup_data):
    app = create_app()
    async with mcp_lifespan():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---------------------------------------------------------------------------
# TestMCPAuthFlowBuiltin
# ---------------------------------------------------------------------------

class TestMCPAuthFlowBuiltin:
    """Exercise the full 9-step MCP authorization flow (builtin auth mode)."""

    async def test_full_authorization_flow(self, client: AsyncClient):
        # ---------------------------------------------------------------
        # Step 1: POST /mcp without token -> 401 + WWW-Authenticate
        # ---------------------------------------------------------------
        resp = await client.post(
            "/mcp",
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            }),
            headers=MCP_HEADERS,
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"

        www_auth = resp.headers.get("www-authenticate", "")
        assert www_auth, "Missing WWW-Authenticate header on 401"

        # Extract resource_metadata URL from WWW-Authenticate header.
        # Format is typically: Bearer resource_metadata="<url>"
        rm_match = re.search(r'resource_metadata="([^"]+)"', www_auth)
        assert rm_match, f"Could not find resource_metadata in WWW-Authenticate: {www_auth}"
        resource_metadata_url = rm_match.group(1)

        # ---------------------------------------------------------------
        # Step 2: GET resource_metadata URL -> PRM (RFC 9728)
        # ---------------------------------------------------------------
        resp = await client.get(resource_metadata_url)
        assert resp.status_code == 200, f"PRM fetch failed: {resp.status_code}"
        prm = resp.json()
        assert "resource" in prm, f"Missing 'resource' in PRM: {prm}"
        assert "authorization_servers" in prm, f"Missing 'authorization_servers' in PRM: {prm}"
        resource_url = prm["resource"]

        # ---------------------------------------------------------------
        # Step 3: GET AS metadata
        # ---------------------------------------------------------------
        resp = await client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200, f"AS metadata fetch failed: {resp.status_code}"
        as_meta = resp.json()
        assert "authorization_endpoint" in as_meta
        assert "token_endpoint" in as_meta
        assert "registration_endpoint" in as_meta

        authorization_endpoint = as_meta["authorization_endpoint"]
        token_endpoint = as_meta["token_endpoint"]
        registration_endpoint = as_meta["registration_endpoint"]

        # ---------------------------------------------------------------
        # Step 4: POST /register -> Dynamic Client Registration
        # ---------------------------------------------------------------
        reg_body = {
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        resp = await client.post(registration_endpoint, json=reg_body)
        assert resp.status_code == 201, f"Client registration failed: {resp.status_code}: {resp.text}"
        reg_data = resp.json()
        client_id = reg_data["client_id"]
        assert client_id, "Empty client_id from registration"

        # ---------------------------------------------------------------
        # Step 5: GET /authorize with PKCE -> 302 to login page
        # ---------------------------------------------------------------
        verifier, challenge = _pkce_pair()

        resp = await client.get(
            authorization_endpoint,
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource_url,
                "state": "test-state",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302, f"Expected 302 from authorize, got {resp.status_code}: {resp.text}"

        login_redirect = resp.headers["location"]
        assert "/mcp-auth/login" in login_redirect, f"Expected redirect to login page, got: {login_redirect}"

        # Extract session_id from the redirect URL
        session_match = re.search(r'session_id=([^&]+)', login_redirect)
        assert session_match, f"Could not find session_id in login redirect: {login_redirect}"
        session_id = session_match.group(1)

        # ---------------------------------------------------------------
        # Step 6: GET /mcp-auth/login?session_id=... -> HTML login form
        # ---------------------------------------------------------------
        resp = await client.get(f"/mcp-auth/login?session_id={session_id}")
        assert resp.status_code == 200, f"Login page fetch failed: {resp.status_code}"
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Sign In" in resp.text or "Authorize" in resp.text
        assert 'name="username"' in resp.text
        assert 'name="password"' in resp.text

        # ---------------------------------------------------------------
        # Step 7: POST /mcp-auth/login/callback -> 302 with code + state
        # ---------------------------------------------------------------
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": session_id,
                "username": "admin",
                "password": "testpass",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302, f"Expected 302 from login callback, got {resp.status_code}: {resp.text}"

        callback_url = resp.headers["location"]
        assert callback_url.startswith(REDIRECT_URI), (
            f"Expected redirect to {REDIRECT_URI}, got: {callback_url}"
        )

        # Extract authorization code from callback URL
        code_match = re.search(r'[?&]code=([^&]+)', callback_url)
        assert code_match, f"Could not find code in callback URL: {callback_url}"
        auth_code = code_match.group(1)

        # Verify state is preserved
        state_match = re.search(r'[?&]state=([^&]+)', callback_url)
        assert state_match, f"Could not find state in callback URL: {callback_url}"
        assert state_match.group(1) == "test-state"

        # ---------------------------------------------------------------
        # Step 8: POST /token -> access_token + refresh_token
        # ---------------------------------------------------------------
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
            },
        )
        assert resp.status_code == 200, f"Token exchange failed: {resp.status_code}: {resp.text}"
        token_data = resp.json()
        assert "access_token" in token_data, f"Missing access_token: {token_data}"
        assert "refresh_token" in token_data, f"Missing refresh_token: {token_data}"
        assert token_data.get("token_type", "").lower() == "bearer"

        access_token = token_data["access_token"]

        # ---------------------------------------------------------------
        # Step 9: POST /mcp with Bearer token -> 200 (MCP response)
        # ---------------------------------------------------------------
        headers = {
            **MCP_HEADERS,
            "Authorization": f"Bearer {access_token}",
        }
        resp = await client.post(
            "/mcp",
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            }),
            headers=headers,
        )
        assert resp.status_code == 200, f"MCP request with valid token failed: {resp.status_code}: {resp.text}"

        events = _parse_sse_response(resp.text)
        assert len(events) >= 1, f"Expected at least 1 SSE event, got {len(events)}"
        result = events[0].get("result", {})
        assert result.get("protocolVersion") == "2025-03-26"
        assert result.get("serverInfo", {}).get("name") == "alibabacloud polardb tool agentic server"


# ---------------------------------------------------------------------------
# TestRouteRegression
# ---------------------------------------------------------------------------

class TestRouteRegression:
    """Verify health and OAuth well-known routes are not shadowed by the MCP mount."""

    async def test_livez_not_shadowed(self, client: AsyncClient):
        resp = await client.get("/livez")
        assert resp.status_code == 200

    async def test_readyz_not_shadowed(self, client: AsyncClient):
        resp = await client.get("/readyz")
        assert resp.status_code == 200

    async def test_prm_accessible(self, client: AsyncClient):
        resp = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["resource"] == "http://localhost:18760/mcp"

    async def test_as_metadata_accessible(self, client: AsyncClient):
        resp = await client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        data = resp.json()
        assert "authorization_endpoint" in data
        assert "registration_endpoint" in data
