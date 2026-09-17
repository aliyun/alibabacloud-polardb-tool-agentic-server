"""Tests for SDK-managed OAuth endpoints.

The MCP SDK manages OAuth routes (AS metadata, dynamic client registration,
authorize, token) via PASAuthProvider. These tests verify the endpoints
are accessible and function correctly.

See test_mcp_auth_flow.py for the full end-to-end authorization flow test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.external_tokens import ExternalTokenIdentity
from server.auth.jwt_manager import reset_keys
from server.config import get_config, reset_config
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.models import (
    Agent,
    AgentUserAssignment,
    AuthProvider,
    Base,
    User,
    UserExternalIdentity,
    UserRole,
    UserWorkspace,
)
from server.models.oauth import (
    ExternalTokenSession,
    OAuthPendingAuth,
    OAuthRegisteredClient,
)
from server.auth.oauth_provider import (
    ACCESS_TOKEN_TYPE,
    PASAuthProvider,
    TOKEN_EXCHANGE_GRANT_TYPE,
)
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
RAW_REDIRECT_URI = "https://CLIENT.example:443/a/../callback"


async def _issue_authorization_code(client, resource=None) -> tuple[str, str, str]:
    registration = await client.post("/register", json={
        "redirect_uris": [RAW_REDIRECT_URI],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    assert registration.status_code == 201
    client_id = registration.json()["client_id"]
    verifier, challenge = _pkce_pair()

    authorization = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            **({"resource": resource} if resource else {}),
            "client_id": client_id,
            "redirect_uri": RAW_REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(12),
        },
        follow_redirects=False,
    )
    assert authorization.status_code == 302
    session_id = parse_qs(
        urlsplit(authorization.headers["location"]).query
    )["session_id"][0]
    login = await client.post(
        "/mcp-auth/login/callback",
        data={
            "session_id": session_id,
            "username": "admin",
            "password": "password",
        },
        follow_redirects=False,
    )
    assert login.status_code == 302
    authorization_code = parse_qs(
        urlsplit(login.headers["location"]).query
    )["code"][0]
    return client_id, verifier, authorization_code


def _oversized_auth_body(path: str) -> bytes:
    padding = "x" * 65_536
    if path == "/register":
        return json.dumps({
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": padding,
        }).encode()
    return urlencode({
        "grant_type": "authorization_code",
        "code": "invalid-code",
        "code_verifier": "invalid-verifier",
        "client_id": "invalid-client",
        "redirect_uri": REDIRECT_URI,
        "padding": padding,
    }).encode()


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
def encryption_key(monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(key).decode(),
    )
    yield key


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
        assert data["token_endpoint_auth_methods_supported"] == [
            "none",
            "client_secret_basic",
            "client_secret_post",
        ]
        assert data["authorization_response_iss_parameter_supported"] is True

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
        assert TOKEN_EXCHANGE_GRANT_TYPE in data["grant_types_supported"]


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

    async def test_register_client_with_token_exchange_grant(self, client):
        response = await client.post("/register", json={
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": [TOKEN_EXCHANGE_GRANT_TYPE],
            "scope": "mcp polarrag",
        })

        assert response.status_code == 201
        registered = response.json()
        assert registered["grant_types"] == [TOKEN_EXCHANGE_GRANT_TYPE]
        assert "redirect_uris" not in registered
        assert registered["response_types"] == []
        assert registered["client_secret"]

    async def test_token_exchange_registration_requires_scope(self, client):
        response = await client.post("/register", json={
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": [TOKEN_EXCHANGE_GRANT_TYPE],
        })

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"

    async def test_combined_registration_requires_scope_for_exchange(
        self,
        client,
    ):
        response = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": [
                "authorization_code",
                "refresh_token",
                TOKEN_EXCHANGE_GRANT_TYPE,
            ],
            "response_types": ["code"],
        })

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"

    async def test_registration_is_rate_limited(self, client):
        for _ in range(10):
            response = await client.post("/register", json={})
            assert response.status_code == 400

        response = await client.post("/register", json={})

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1
        assert response.json() == {
            "error": "rate_limit_exceeded",
            "error_description": "Too many authentication requests.",
        }


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

    async def test_redirect_uri_match_uses_registered_raw_string(
        self, client
    ):
        registered_uri = "https://CLIENT.example:443/a/../callback"
        normalized_uri = "https://client.example/callback"
        registration = await client.post("/register", json={
            "redirect_uris": [registered_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]
        async with engine_mod._session_factory() as session:
            stored_client = await session.get(
                OAuthRegisteredClient, client_id
            )
            assert stored_client is not None
            assert registered_uri in stored_client.redirect_uris
        _, challenge = _pkce_pair()
        request = {
            "response_type": "code",
            "client_id": client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "test-state",
        }

        normalized = await client.get(
            "/authorize",
            params={**request, "redirect_uri": normalized_uri},
            follow_redirects=False,
        )

        assert normalized.status_code == 400
        async with engine_mod._session_factory() as session:
            assert list(
                (await session.execute(select(OAuthPendingAuth))).scalars()
            ) == []

        exact = await client.get(
            "/authorize",
            params={**request, "redirect_uri": registered_uri},
            follow_redirects=False,
        )
        assert exact.status_code == 302
        assert "/mcp-auth/login" in exact.headers["location"]

    async def test_authorize_allows_dynamic_loopback_port(self, client):
        registered_uri = "http://127.0.0.1:49152/callback"
        requested_uri = "http://127.0.0.1:61234/callback"
        registration = await client.post("/register", json={
            "redirect_uris": [registered_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert registration.status_code == 201
        _, challenge = _pkce_pair()
        authorization = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registration.json()["client_id"],
                "redirect_uri": requested_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "loopback-state",
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 302
        assert "/mcp-auth/login" in authorization.headers["location"]


# ---------------------------------------------------------------------------
# TestTokenEndpoint
# ---------------------------------------------------------------------------

class TestTokenEndpoint:
    """Test the SDK-managed token endpoint error handling."""

    async def test_exchanges_external_token_for_pas_tokens(
        self,
        client,
        monkeypatch,
    ):
        config = get_config()
        config.auth.external_token_trust.enabled = True
        config.auth.external_token_trust.provider = "oauth2_userinfo"
        config.auth.external_token_trust.config_digest = "test-digest"
        config.server.public_base_url = "https://pas.invalid"

        async def validate(_self, token):
            assert token == "external-access-token"
            return ExternalTokenIdentity(
                provider_type="oauth2_userinfo",
                provider_key="test-idp",
                provider_fingerprint=_self._base_fingerprint(),
                subject="external-user",
                display_name="External User",
                email="external@example.com",
                scopes=("mcp",),
                expires_at=None,
            )

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
            validate,
        )
        registration = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": [
                "authorization_code",
                "refresh_token",
                TOKEN_EXCHANGE_GRANT_TYPE,
            ],
            "response_types": ["code"],
            "scope": "mcp",
        })
        assert registration.status_code == 201

        response = await client.post(
            "/token",
            data={
                "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
                "client_id": registration.json()["client_id"],
                "subject_token": "external-access-token",
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "resource": "https://pas.invalid/mcp",
                "scope": "mcp",
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["issued_token_type"] == ACCESS_TOKEN_TYPE
        assert payload["token_type"] == "Bearer"
        assert payload["access_token"]
        assert "refresh_token" not in payload
        assert payload["expires_in"] == 28800
        async with engine_mod._session_factory() as session:
            external_sessions = list(
                (await session.execute(select(ExternalTokenSession))).scalars()
            )
        assert external_sessions == []

    async def test_exchanges_feishu_token_with_direct_identity_context(
        self,
        client,
        monkeypatch,
    ):
        config = get_config()
        config.auth.external_token_trust.enabled = True
        config.auth.external_token_trust.provider = "feishu"
        config.auth.external_token_trust.config_digest = "test-digest"
        config.server.public_base_url = "https://pas.invalid"

        async def validate(_self, token, **context):
            assert token == "feishu-user-access-token"
            assert context == {
                "identity_source_id": "feishu-source-id",
                "feishu_user_id": "ou_123",
                "feishu_union_id": "on_123",
            }
            return ExternalTokenIdentity(
                provider_type="feishu",
                provider_key="feishu:tenant-001",
                provider_fingerprint=_self._base_fingerprint(),
                subject="ou_123",
                display_name="Feishu User",
                email=None,
                scopes=(),
                expires_at=None,
            )

        async def resolve_user(_self, session, _identity):
            return (
                await session.execute(
                    select(User).where(User.external_id == "admin")
                )
            ).scalar_one()

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
            validate,
        )
        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.resolve_user",
            resolve_user,
        )
        registration = await client.post("/register", json={
            "token_endpoint_auth_method": "none",
            "grant_types": [TOKEN_EXCHANGE_GRANT_TYPE],
            "scope": "mcp",
        })
        assert registration.status_code == 201

        response = await client.post(
            "/token",
            data={
                "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
                "client_id": registration.json()["client_id"],
                "subject_token": "feishu-user-access-token",
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "identity_source_id": "feishu-source-id",
                "feishu_user_id": "ou_123",
                "feishu_union_id": "on_123",
                "resource": "https://pas.invalid/mcp",
                "scope": "mcp",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    async def test_external_auth_alias_issues_platform_token_for_default_agent(
        self,
        client,
        monkeypatch,
    ):
        config = get_config()
        config.auth.external_token_trust.enabled = True
        config.auth.external_token_trust.provider = "oauth2_userinfo"
        config.auth.external_token_trust.config_digest = "test-digest"
        config.server.public_base_url = "https://pas.invalid"

        async def validate(_self, token):
            assert token == "external-assertion"
            return ExternalTokenIdentity(
                provider_type="oauth2_introspection_userinfo",
                provider_key="external-provider",
                provider_fingerprint=_self._base_fingerprint(),
                subject="external-user",
                display_name="External User",
                email=None,
                scopes=("provider.profile",),
                expires_at=None,
                scope_authoritative=False,
                requires_existing_mapping=True,
            )

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
            validate,
        )
        async with engine_mod._session_factory() as session:
            user = User(
                external_id="external-provider:external-user",
                display_name="External User",
                auth_provider=AuthProvider.OIDC,
                role=UserRole.MEMBER,
            )
            agent = Agent(name="External User Agent")
            session.add_all([user, agent])
            await session.flush()
            session.add_all([
                UserExternalIdentity(
                    user_id=user.id,
                    identity_provider="external-provider",
                    external_subject="external-user",
                ),
                AgentUserAssignment(
                    agent_id=agent.id,
                    user_id=user.id,
                    is_direct=True,
                ),
                UserWorkspace(
                    user_id=user.id,
                    default_agent_id=agent.id,
                ),
            ])
            await session.commit()
            user_id = user.id
            agent_id = agent.id

        async def resolve_user(_self, session, identity):
            assert identity.provider_type == "oauth2_introspection_userinfo"
            return await session.get(User, user_id)

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.resolve_user",
            resolve_user,
        )

        registration = await client.post("/register", json={
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": [TOKEN_EXCHANGE_GRANT_TYPE],
            "scope": "polarrag",
        })
        assert registration.status_code == 201
        registered = registration.json()
        external_client_id = registered["client_id"]
        credentials = base64.b64encode(
            (
                f"{external_client_id}:"
                f"{registered['client_secret']}"
            ).encode()
        ).decode()

        response = await client.post(
            "/api/v1/external-auth/token",
            data={
                "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
                "client_id": external_client_id,
                "subject_token": "external-assertion",
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "resource": "https://pas.invalid/api/v1",
                "scope": "polarrag",
            },
            headers={"Authorization": f"Basic {credentials}"},
        )

        assert response.status_code == 200, response.text
        assert "refresh_token" not in response.json()
        provider = PASAuthProvider(
            engine_mod._session_factory,
            config,
        )
        access_token = await provider.load_access_token(
            response.json()["access_token"]
        )
        assert access_token is not None
        assert access_token.client_id == external_client_id
        assert access_token.subject == f"user:{user_id}"
        assert access_token.resource == "https://pas.invalid/api/v1"
        assert access_token.scopes == ["polarrag"]
        assert access_token.claims["agent_id"] == agent_id

    async def test_exchange_accepts_basic_auth_without_form_client_id(
        self,
        client,
        monkeypatch,
    ):
        config = get_config()
        config.auth.external_token_trust.enabled = True
        config.auth.external_token_trust.provider = "oauth2_userinfo"
        config.auth.external_token_trust.config_digest = "test-digest"
        config.server.public_base_url = "https://pas.invalid"

        async def validate(_self, token):
            assert token == "external-access-token"
            return ExternalTokenIdentity(
                provider_type="oauth2_userinfo",
                provider_key="test-idp",
                provider_fingerprint=_self._base_fingerprint(),
                subject="external-user",
                display_name="External User",
                email=None,
                scopes=("mcp",),
                expires_at=None,
            )

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
            validate,
        )
        registration = await client.post("/register", json={
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": [TOKEN_EXCHANGE_GRANT_TYPE],
            "scope": "mcp",
        })
        assert registration.status_code == 201
        registered = registration.json()
        credentials = base64.b64encode(
            (
                f"{registered['client_id']}:"
                f"{registered['client_secret']}"
            ).encode()
        ).decode()

        response = await client.post(
            "/token",
            data={
                "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
                "subject_token": "external-access-token",
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "resource": "https://pas.invalid/mcp",
                "scope": "mcp",
            },
            headers={"Authorization": f"Basic {credentials}"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["issued_token_type"] == ACCESS_TOKEN_TYPE

    async def test_exchange_rejects_client_without_exchange_grant(
        self,
        client,
        monkeypatch,
    ):
        called = False

        async def validate(_self, _token):
            nonlocal called
            called = True
            raise AssertionError("external validation must not run")

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
            validate,
        )
        registration = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "mcp",
        })
        assert registration.status_code == 201

        response = await client.post(
            "/token",
            data={
                "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
                "client_id": registration.json()["client_id"],
                "subject_token": "external-access-token",
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "resource": "https://pas.invalid/mcp",
                "scope": "mcp",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "unauthorized_client"
        assert called is False

    async def test_exchange_rejects_mismatched_basic_and_form_client(
        self,
        client,
    ):
        registration = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert registration.status_code == 201
        registered = registration.json()
        credentials = base64.b64encode(
            (
                f"{registered['client_id']}:"
                f"{registered['client_secret']}"
            ).encode()
        ).decode()

        response = await client.post(
            "/token",
            data={
                "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
                "client_id": "different-client",
                "subject_token": "external-access-token",
                "subject_token_type": ACCESS_TOKEN_TYPE,
            },
            headers={"Authorization": f"Basic {credentials}"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"

    async def test_subject_token_without_grant_type_is_not_inferred(
        self,
        client,
        monkeypatch,
    ):
        called = False

        async def validate(_self, _token):
            nonlocal called
            called = True
            raise AssertionError("external validation must not run")

        monkeypatch.setattr(
            "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
            validate,
        )
        registration = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": [
                "authorization_code",
                "refresh_token",
                TOKEN_EXCHANGE_GRANT_TYPE,
            ],
            "response_types": ["code"],
            "scope": "mcp",
        })
        assert registration.status_code == 201
        response = await client.post(
            "/token",
            data={
                "client_id": registration.json()["client_id"],
                "subject_token": "external-access-token",
                "subject_token_type": ACCESS_TOKEN_TYPE,
            },
        )

        assert response.status_code == 400
        assert called is False

    async def test_token_redirect_uri_match_uses_authorization_raw_string(
        self, client
    ):
        normalized_uri = "https://client.example/callback"
        client_id, verifier, authorization_code = (
            await _issue_authorization_code(client)
        )
        token_request = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "code_verifier": verifier,
            "client_id": client_id,
        }

        normalized = await client.post(
            "/token",
            data={**token_request, "redirect_uri": normalized_uri},
        )
        exact = await client.post(
            "/token",
            data={**token_request, "redirect_uri": RAW_REDIRECT_URI},
        )

        assert normalized.status_code == 400
        assert exact.status_code == 200

    async def test_client_secret_basic_without_form_client_id(
        self, client
    ):
        registration = await client.post("/register", json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        })
        assert registration.status_code == 201
        registered = registration.json()
        client_id = registered["client_id"]
        verifier, challenge = _pkce_pair()
        authorization = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "basic-state",
            },
            follow_redirects=False,
        )
        session_id = parse_qs(
            urlsplit(authorization.headers["location"]).query
        )["session_id"][0]
        login = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": session_id,
                "username": "admin",
                "password": "password",
            },
            follow_redirects=False,
        )
        authorization_code = parse_qs(
            urlsplit(login.headers["location"]).query
        )["code"][0]
        credentials = base64.b64encode(
            f"{client_id}:{registered['client_secret']}".encode()
        ).decode()
        response = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Authorization": f"Basic {credentials}"},
        )
        assert response.status_code == 200
        assert response.json()["token_type"] == "Bearer"

    @pytest.mark.parametrize(
        ("duplicate_field", "duplicate_value"),
        [
            ("grant_type", "authorization_code"),
            ("code", "different-code"),
            ("redirect_uri", "https://client.example/callback"),
        ],
    )
    async def test_authorization_code_rejects_duplicate_critical_parameter(
        self,
        client,
        duplicate_field,
        duplicate_value,
    ):
        client_id, verifier, authorization_code = (
            await _issue_authorization_code(client)
        )
        fields = [
            ("grant_type", "authorization_code"),
            ("code", authorization_code),
            ("code_verifier", verifier),
            ("client_id", client_id),
            ("redirect_uri", RAW_REDIRECT_URI),
            (duplicate_field, duplicate_value),
        ]

        duplicate = await client.post(
            "/token",
            content=urlencode(fields),
            headers={
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        exact = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": RAW_REDIRECT_URI,
            },
        )

        assert duplicate.status_code == 400
        assert duplicate.headers["cache-control"] == "no-store"
        assert "access_token" not in duplicate.text
        assert exact.status_code == 200

    @pytest.mark.parametrize(
        ("path", "content_type"),
        [
            ("/register", "application/json"),
            ("/token", "application/x-www-form-urlencoded"),
        ],
    )
    async def test_auth_endpoint_rejects_declared_oversized_body(
        self,
        client,
        path,
        content_type,
    ):
        response = await client.post(
            path,
            content=b"{}" if path == "/register" else b"grant_type=x",
            headers={
                "content-type": content_type,
                "content-length": "65537",
            },
        )

        assert response.status_code == 413
        assert response.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize(
        ("path", "content_type"),
        [
            ("/register", "application/json"),
            ("/token", "application/x-www-form-urlencoded"),
        ],
    )
    async def test_auth_endpoint_rejects_chunked_oversized_body(
        self,
        client,
        path,
        content_type,
    ):
        body = _oversized_auth_body(path)

        async def chunks():
            midpoint = len(body) // 2
            yield body[:midpoint]
            yield body[midpoint:]

        response = await client.post(
            path,
            content=chunks(),
            headers={
                "content-type": content_type,
                "content-length": "1",
            },
        )

        assert response.status_code == 413
        assert response.headers["cache-control"] == "no-store"

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

    async def test_token_endpoint_is_rate_limited(self, client):
        request = {
            "grant_type": "authorization_code",
            "code": "invalid-code",
            "code_verifier": "test-verifier",
            "client_id": "nonexistent",
            "redirect_uri": REDIRECT_URI,
        }
        for _ in range(20):
            response = await client.post("/token", data=request)
            assert response.status_code in (400, 401)

        response = await client.post("/token", data=request)

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1
        assert "invalid-code" not in response.text


async def test_personal_oauth_http_authorization_pkce_and_refresh(client):
    resource = get_config().server.public_base_url.rstrip('/') + '/mcp/personal'
    client_id, verifier, code = await _issue_authorization_code(client, resource)
    response = await client.post('/token', data={
        'grant_type':'authorization_code','client_id':client_id,'code':code,
        'code_verifier':verifier,'redirect_uri':RAW_REDIRECT_URI,'resource':resource,
    })
    assert response.status_code == 200
    token = response.json()
    headers = {'Authorization':'Bearer '+token['access_token'], 'Accept':'application/json, text/event-stream'}
    rpc = {'jsonrpc':'2.0','id':1,'method':'tools/list'}
    assert (await client.post('/mcp/personal',headers=headers,json=rpc)).status_code == 200
    assert (await client.post('/mcp',headers=headers,json=rpc)).status_code == 401
    response = await client.post('/token',data={'grant_type':'refresh_token','client_id':client_id,'refresh_token':token['refresh_token'],'resource':resource})
    assert response.status_code == 200
    headers['Authorization'] = 'Bearer ' + response.json()['access_token']
    assert (await client.post('/mcp/personal',headers=headers,json=rpc)).status_code == 200
