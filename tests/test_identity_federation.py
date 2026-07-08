from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.identity_federation import IdentityFederation, UserIdentity
from server.config import OIDCConfig, reset_config
from server.auth.jwt_manager import reset_keys
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.models import Base, User
from server.models.oauth import OAuthPendingAuth, OAuthAuthorizationCode


@pytest.fixture(autouse=True)
def clean():
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()


@pytest.fixture
def encryption_key(monkeypatch):
    key = os.urandom(32)
    key_b64 = base64.b64encode(key).decode()
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", key_b64)
    return key_b64


@pytest.fixture
async def test_engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(test_engine):
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with engine_mod._session_factory() as s:
        yield s


class TestEndpointDiscovery:
    async def test_discover_from_url(self):
        config = OIDCConfig(
            discovery_url="https://sso.example.com/.well-known/openid-configuration",
            client_id="test",
        )
        federation = IdentityFederation(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "authorization_endpoint": "https://sso.example.com/authorize",
            "token_endpoint": "https://sso.example.com/token",
            "userinfo_endpoint": "https://sso.example.com/userinfo",
            "jwks_uri": "https://sso.example.com/jwks",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("server.auth.identity_federation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            endpoints = await federation.discover_endpoints()
            assert endpoints.authorization_endpoint == "https://sso.example.com/authorize"
            assert endpoints.token_endpoint == "https://sso.example.com/token"
            assert endpoints.userinfo_endpoint == "https://sso.example.com/userinfo"
            assert endpoints.jwks_uri == "https://sso.example.com/jwks"

    async def test_manual_endpoints(self):
        config = OIDCConfig(
            authorization_endpoint="https://login.example.com/oauth2/auth.htm",
            token_endpoint="https://login.example.com/rpc/oauth2/access_token.json",
            userinfo_endpoint="https://login.example.com/rpc/oauth2/user_info.json",
            client_id="mcp-server",
        )
        federation = IdentityFederation(config)
        endpoints = await federation.discover_endpoints()
        assert endpoints.authorization_endpoint == "https://login.example.com/oauth2/auth.htm"
        assert endpoints.token_endpoint == "https://login.example.com/rpc/oauth2/access_token.json"
        assert endpoints.userinfo_endpoint == "https://login.example.com/rpc/oauth2/user_info.json"

    async def test_missing_manual_endpoints_raises(self):
        config = OIDCConfig(client_id="test")
        federation = IdentityFederation(config)
        with pytest.raises(ValueError, match="discovery_url or manual"):
            await federation.discover_endpoints()

    async def test_caches_result(self):
        config = OIDCConfig(
            authorization_endpoint="https://a.com/auth",
            token_endpoint="https://a.com/token",
            client_id="test",
        )
        federation = IdentityFederation(config)
        ep1 = await federation.discover_endpoints()
        ep2 = await federation.discover_endpoints()
        assert ep1 is ep2  # same object, cached


class TestBuildAuthorizeUrl:
    async def test_builds_url_with_params(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            client_id="my-client",
            scopes=["openid", "profile"],
        )
        federation = IdentityFederation(config)
        await federation.discover_endpoints()
        url, verifier = federation.build_authorize_url(
            redirect_uri="http://localhost/callback",
            state="test-state",
            nonce="test-nonce",
        )
        assert "client_id=my-client" in url
        assert "state=test-state" in url
        assert "nonce=test-nonce" in url
        assert "scope=openid+profile" in url
        assert verifier is None  # idp_pkce not enabled

    async def test_build_url_without_nonce(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            client_id="my-client",
        )
        federation = IdentityFederation(config)
        await federation.discover_endpoints()
        url, verifier = federation.build_authorize_url(
            redirect_uri="http://localhost/callback",
            state="s1",
        )
        assert "nonce" not in url
        assert "state=s1" in url
        assert verifier is None

    async def test_build_url_with_idp_pkce(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            client_id="my-client",
            idp_pkce=True,
        )
        federation = IdentityFederation(config)
        await federation.discover_endpoints()
        url, verifier = federation.build_authorize_url(
            redirect_uri="http://localhost/callback",
            state="s1",
        )
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert verifier is not None
        assert len(verifier) > 20

    async def test_build_url_before_discover_raises(self):
        config = OIDCConfig(
            authorization_endpoint="https://a.com/auth",
            token_endpoint="https://a.com/token",
            client_id="test",
        )
        federation = IdentityFederation(config)
        with pytest.raises(RuntimeError, match="discover_endpoints"):
            federation.build_authorize_url("http://x/cb", "state")


class TestExchangeCode:
    async def test_exchange_code_posts_to_token_endpoint(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            client_id="my-client",
            client_secret="test-client-credential",
        )
        federation = IdentityFederation(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "at-123",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("server.auth.identity_federation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            tokens = await federation.exchange_code("auth-code-xyz", "http://localhost/callback")
            assert tokens["access_token"] == "at-123"
            # Verify the POST was made with correct data
            call_kwargs = mock_client.post.call_args
            assert call_kwargs.args[0] == "https://sso.example.com/token"
            assert call_kwargs.kwargs["data"]["code"] == "auth-code-xyz"
            assert call_kwargs.kwargs["data"]["client_id"] == "my-client"
            assert call_kwargs.kwargs["data"]["client_secret"] == "test-client-credential"


class TestExtractUserIdentity:
    async def test_extract_via_userinfo_bearer(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            userinfo_endpoint="https://sso.example.com/userinfo",
            client_id="test",
        )
        federation = IdentityFederation(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "user-001",
            "name": "Alice",
            "email": "alice@example.com",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("server.auth.identity_federation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            identity = await federation.extract_user_identity(
                {"access_token": "at-xxx"}
            )
            assert identity.subject == "user-001"
            assert identity.display_name == "Alice"
            assert identity.email == "alice@example.com"
            # Verify bearer header was used
            call_kwargs = mock_client.get.call_args
            assert "Bearer at-xxx" in call_kwargs.kwargs["headers"]["Authorization"]

    async def test_extract_via_userinfo_form_post(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            userinfo_endpoint="https://sso.example.com/userinfo",
            client_id="test",
            userinfo_token_method="form_post",
        )
        federation = IdentityFederation(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "user-002",
            "name": "Bob",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("server.auth.identity_federation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            identity = await federation.extract_user_identity(
                {"access_token": "at-yyy"}
            )
            assert identity.subject == "user-002"
            assert identity.display_name == "Bob"
            # Verify form post was used
            call_kwargs = mock_client.post.call_args
            assert call_kwargs.kwargs["data"]["access_token"] == "at-yyy"

    async def test_extract_via_id_token_requires_jwks(self):
        """id_token fallback without jwks_uri must raise ValueError."""
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            client_id="test",
        )
        federation = IdentityFederation(config)

        import json
        import base64

        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "user-003"}).encode()
        ).rstrip(b"=")
        fake_id_token = f"{header.decode()}.{payload.decode()}."

        with pytest.raises(ValueError, match="Cannot verify id_token"):
            await federation.extract_user_identity(
                {"id_token": fake_id_token}
            )

    async def test_extract_via_id_token_with_jwks(self):
        """id_token fallback with jwks_uri verifies signature."""
        import base64
        from jose import jwt as jose_jwt
        from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
        from cryptography.hazmat.primitives import serialization

        private_key = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        pub_numbers = private_key.public_key().public_numbers()

        def _int_to_b64(n, length):
            return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

        jwks = {"keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "n": _int_to_b64(pub_numbers.n, (pub_numbers.n.bit_length() + 7) // 8),
            "e": _int_to_b64(pub_numbers.e, 3),
        }]}

        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            jwks_uri="https://sso.example.com/.well-known/jwks.json",
            client_id="test-client",
        )
        federation = IdentityFederation(config)

        id_token = jose_jwt.encode(
            {"sub": "user-003", "name": "Charlie", "email": "c@e.com", "aud": "test-client"},
            private_pem,
            algorithm="RS256",
        )

        mock_jwks_response = MagicMock()
        mock_jwks_response.json.return_value = jwks
        mock_jwks_response.raise_for_status = MagicMock()

        with patch("server.auth.identity_federation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_jwks_response)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            identity = await federation.extract_user_identity(
                {"id_token": id_token}
            )

        assert identity.subject == "user-003"
        assert identity.display_name == "Charlie"
        assert identity.email == "c@e.com"

    async def test_extract_raises_when_no_source(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            client_id="test",
        )
        federation = IdentityFederation(config)
        with pytest.raises(ValueError, match="Cannot extract user identity"):
            await federation.extract_user_identity({"access_token": "at"})

    async def test_extract_raises_when_claim_missing(self):
        config = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            userinfo_endpoint="https://sso.example.com/userinfo",
            client_id="test",
            user_id_claim="employee_id",  # custom claim
        )
        federation = IdentityFederation(config)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "sub": "user-001",  # has sub but not employee_id
            "name": "Alice",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("server.auth.identity_federation.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client

            with pytest.raises(ValueError, match="employee_id"):
                await federation.extract_user_identity({"access_token": "at"})


class TestUserMapping:
    async def test_creates_new_user(self, session, encryption_key):
        config = OIDCConfig(client_id="test")
        federation = IdentityFederation(config, provider_name="test-idp")
        identity = UserIdentity(
            subject="emp-123", display_name="Test User", email="test@example.com"
        )
        user = await federation.find_or_create_user(session, identity)
        assert user.id is not None
        assert user.display_name == "Test User"
        assert user.external_id == "test-idp:emp-123"

    async def test_returns_existing_user(self, session, encryption_key):
        config = OIDCConfig(client_id="test")
        federation = IdentityFederation(config, provider_name="test-idp")
        identity = UserIdentity(subject="emp-456", display_name="Existing")
        user1 = await federation.find_or_create_user(session, identity)
        user2 = await federation.find_or_create_user(session, identity)
        assert user1.id == user2.id

    async def test_uses_subject_as_fallback_display_name(self, session, encryption_key):
        config = OIDCConfig(client_id="test")
        federation = IdentityFederation(config, provider_name="test-idp")
        identity = UserIdentity(subject="emp-789")  # no display_name
        user = await federation.find_or_create_user(session, identity)
        assert user.display_name == "emp-789"

    async def test_auto_assigns_default_department(self, session, encryption_key):
        from server.models.department import Department
        from server.models.binding import UserDepartment

        dept = Department(name="default", description="Default department")
        session.add(dept)
        await session.commit()
        dept_id = dept.id

        with patch.dict(os.environ, {"PAS_AUTH_DEFAULT_DEPARTMENT": "default", "PAS_SERVER_DEV_MODE": "true"}):
            reset_config()
            config = OIDCConfig(client_id="test")
            federation = IdentityFederation(config, provider_name="test-idp")
            identity = UserIdentity(subject="new-user-001", display_name="New User")
            user = await federation.find_or_create_user(session, identity)

        membership = (await session.execute(
            select(UserDepartment).where(
                UserDepartment.user_id == user.id,
                UserDepartment.department_id == dept_id,
            )
        )).scalar_one_or_none()
        assert membership is not None
        assert membership.is_primary is True


class TestOIDCCallback:
    """Test the OIDC callback handler with mocked IdP responses."""

    async def test_callback_creates_user_and_redirects(
        self, session, encryption_key, monkeypatch
    ):
        """Full OIDC callback flow: pending auth -> exchange code -> create user -> redirect."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route
        from server.auth.auth_routes import handle_oidc_callback

        # 1. Insert a pending auth record with idp_state
        idp_state = "test-idp-state-123"
        pending = OAuthPendingAuth(
            client_id="test-client",
            redirect_uri="http://localhost:18761/callback",
            code_challenge="test_challenge_abc",
            code_challenge_method="S256",
            resource="http://localhost:18760/mcp",
            scopes='["read", "write"]',
            state="mcp-client-state",
            idp_state=idp_state,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        session.add(pending)
        await session.commit()

        # 2. Mock get_config to return OIDC config
        mock_config = MagicMock()
        mock_config.server.public_base_url = "http://localhost:18760"
        mock_config.auth.oidc = OIDCConfig(
            authorization_endpoint="https://sso.example.com/authorize",
            token_endpoint="https://sso.example.com/token",
            userinfo_endpoint="https://sso.example.com/userinfo",
            client_id="test-client",
            client_secret="test-client-credential",
        )
        monkeypatch.setattr(
            "server.config.get_config", lambda: mock_config
        )

        # 3. Mock IdentityFederation methods
        mock_identity = UserIdentity(
            subject="user-from-idp", display_name="IdP User", email="idp@example.com"
        )
        mock_user = MagicMock(spec=User)
        mock_user.id = "local-user-id-001"

        mock_federation = AsyncMock(spec=IdentityFederation)
        mock_federation.discover_endpoints = AsyncMock()
        mock_federation.exchange_code = AsyncMock(
            return_value={"access_token": "idp-at-123", "id_token": "fake-jwt"}
        )
        mock_federation.extract_user_identity = AsyncMock(
            return_value=mock_identity
        )
        mock_federation.find_or_create_user = AsyncMock(return_value=mock_user)

        with patch(
            "server.auth.identity_federation.IdentityFederation",
            return_value=mock_federation,
        ):
            # 4. Build a minimal Starlette app with the OIDC callback route
            app = Starlette(
                routes=[Route("/auth/oidc/callback", handle_oidc_callback)],
            )
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                f"/auth/oidc/callback?code=idp-code-xyz&state={idp_state}",
                follow_redirects=False,
            )

        # 5. Verify success page with redirect to MCP client
        assert resp.status_code == 200
        body = resp.text
        assert "Authorization Successful" in body
        assert "http://localhost:18761/callback?" in body
        assert "code=" in body
        assert "state=mcp-client-state" in body

        # 6. Verify an authorization code record was created in DB
        result = await session.execute(
            select(OAuthAuthorizationCode).where(
                OAuthAuthorizationCode.client_id == "test-client"
            )
        )
        code_record = result.scalar_one_or_none()
        assert code_record is not None
        assert code_record.user_id == "local-user-id-001"

        # 7. Verify pending auth was deleted
        result = await session.execute(
            select(OAuthPendingAuth).where(
                OAuthPendingAuth.idp_state == idp_state
            )
        )
        assert result.scalar_one_or_none() is None

    async def test_callback_missing_code_returns_400(self):
        """Callback without code parameter returns 400."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route
        from server.auth.auth_routes import handle_oidc_callback

        app = Starlette(
            routes=[Route("/auth/oidc/callback", handle_oidc_callback)],
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/auth/oidc/callback?state=some-state")
        assert resp.status_code == 400

    async def test_callback_missing_state_returns_400(self):
        """Callback without state parameter returns 400."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route
        from server.auth.auth_routes import handle_oidc_callback

        app = Starlette(
            routes=[Route("/auth/oidc/callback", handle_oidc_callback)],
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/auth/oidc/callback?code=some-code")
        assert resp.status_code == 400

    async def test_callback_invalid_state_returns_404(self, session, encryption_key):
        """Callback with unknown idp_state returns 404."""
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route
        from server.auth.auth_routes import handle_oidc_callback

        app = Starlette(
            routes=[Route("/auth/oidc/callback", handle_oidc_callback)],
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/auth/oidc/callback?code=some-code&state=nonexistent-state"
        )
        assert resp.status_code == 404
