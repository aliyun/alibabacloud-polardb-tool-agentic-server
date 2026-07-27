"""Tests for SDK-managed OAuth endpoints.

The MCP SDK manages OAuth routes (AS metadata, dynamic client registration,
authorize, token) via PASAuthProvider. These tests verify the endpoints
are accessible and function correctly.

See test_mcp_auth_flow.py for the full end-to-end authorization flow test.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
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


REDIRECT_URI = "http://localhost:18761/callback"


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
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()


@pytest.fixture
def encryption_key():
    key = os.urandom(32)
    os.environ["PAS_ENCRYPTION_KEY"] = base64.b64encode(key).decode()
    yield key
    if "PAS_ENCRYPTION_KEY" in os.environ:
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
            password_hash=hash_password("password"),
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
# TestOAuthMetadata
# ---------------------------------------------------------------------------

class TestOAuthMetadata:
    """Test the SDK-managed OAuth AS metadata endpoint."""

    async def test_well_known_endpoint(self, client):
        resp = await client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        data = resp.json()
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data
        assert "registration_endpoint" in data
        assert data["code_challenge_methods_supported"] == ["S256"]

    async def test_response_types_supported(self, client):
        resp = await client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        data = resp.json()
        assert "code" in data["response_types_supported"]

    async def test_grant_types_supported(self, client):
        resp = await client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        data = resp.json()
        assert "authorization_code" in data["grant_types_supported"]
        assert "refresh_token" in data["grant_types_supported"]


# ---------------------------------------------------------------------------
# TestDynamicRegistration
# ---------------------------------------------------------------------------

class TestDynamicRegistration:
    """Test the SDK-managed dynamic client registration endpoint."""

    async def test_register_client(self, client):
        resp = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "client_id" in data
        assert data["client_id"]  # non-empty

    async def test_register_client_returns_metadata(self, client):
        resp = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["redirect_uris"] == [REDIRECT_URI]
        assert data["token_endpoint_auth_method"] == "none"


# ---------------------------------------------------------------------------
# TestAuthorizeEndpoint
# ---------------------------------------------------------------------------

class TestAuthorizeEndpoint:
    """Test the SDK-managed authorize endpoint."""

    async def _register_client(self, client: AsyncClient) -> str:
        resp = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert resp.status_code == 201, f"Registration failed: {resp.status_code}: {resp.text}"
        return resp.json()["client_id"]

    async def test_authorize_redirects_to_login(self, client):
        client_id = await self._register_client(client)
        _, challenge = _pkce_pair()

        resp = await client.get("/authorize", params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "test-state",
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "/mcp-auth/login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# TestTokenEndpoint
# ---------------------------------------------------------------------------

class TestTokenEndpoint:
    """Test the SDK-managed token endpoint error handling."""

    async def test_invalid_code_rejected(self, client):
        resp = await client.post("/token", data={
            "grant_type": "authorization_code",
            "code": "invalid-code",
            "code_verifier": "test-verifier",
            "client_id": "nonexistent",
            "redirect_uri": REDIRECT_URI,
        })
        # SDK returns 401 for unrecognized client_id
        assert resp.status_code in (400, 401)
