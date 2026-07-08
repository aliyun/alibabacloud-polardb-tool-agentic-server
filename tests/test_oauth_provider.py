from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull

from server.auth.jwt_manager import reset_keys
from server.config import AppConfig, reset_config
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.models import Base
from server.models.oauth import OAuthAuthorizationCode
from server.auth.oauth_provider import PASAuthProvider


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    reset_keys()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    yield
    reset_config()
    reset_keys()
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
async def session_factory(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    engine_mod._engine = test_engine
    engine_mod._session_factory = factory
    return factory


@pytest.fixture
def provider(session_factory, encryption_key) -> PASAuthProvider:
    config = AppConfig(server={"dev_mode": True})
    return PASAuthProvider(session_factory=session_factory, config=config)


class TestClientRegistration:
    async def test_register_and_get_client(self, provider: PASAuthProvider):
        now = int(time.time())
        client_info = OAuthClientInformationFull(
            client_id="test-client-001",
            client_secret="stored-client-credential",
            client_id_issued_at=now,
            client_secret_expires_at=0,
            redirect_uris=["http://localhost:8080/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
            scope="read write",
            client_name="Test App",
        )

        await provider.register_client(client_info)

        result = await provider.get_client("test-client-001")
        assert result is not None
        assert result.client_id == "test-client-001"
        assert result.client_secret == "stored-client-credential"
        assert result.client_id_issued_at == now
        assert result.client_secret_expires_at == 0
        assert len(result.redirect_uris) == 1
        assert str(result.redirect_uris[0]) == "http://localhost:8080/callback"
        assert result.grant_types == ["authorization_code", "refresh_token"]
        assert result.response_types == ["code"]
        assert result.token_endpoint_auth_method == "client_secret_post"
        assert result.scope == "read write"
        assert result.client_name == "Test App"

    async def test_get_nonexistent_client(self, provider: PASAuthProvider):
        result = await provider.get_client("does-not-exist")
        assert result is None

    async def test_register_public_client_no_secret(
        self, provider: PASAuthProvider
    ):
        client_info = OAuthClientInformationFull(
            client_id="public-client-001",
            client_secret=None,
            redirect_uris=["http://localhost:3000/callback"],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            client_name="Public App",
        )

        await provider.register_client(client_info)

        result = await provider.get_client("public-client-001")
        assert result is not None
        assert result.client_id == "public-client-001"
        assert result.client_secret is None
        assert result.client_name == "Public App"
        assert result.token_endpoint_auth_method == "none"
        assert len(result.redirect_uris) == 1


@pytest.fixture
async def registered_client(provider: PASAuthProvider) -> OAuthClientInformationFull:
    now = int(time.time())
    client_info = OAuthClientInformationFull(
        client_id="authorize-test-client",
        client_secret="test-client-credential",
        client_id_issued_at=now,
        client_secret_expires_at=0,
        redirect_uris=["http://localhost/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        scope="openid",
        client_name="Authorize Test App",
    )
    await provider.register_client(client_info)
    return client_info


class TestAuthorize:
    async def test_authorize_builtin_returns_login_url(
        self, provider: PASAuthProvider, registered_client: OAuthClientInformationFull
    ):
        url = await provider.authorize(
            registered_client,
            AuthorizationParams(
                state="state-123",
                scopes=["openid"],
                code_challenge="challenge-abc",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://localhost:18760/mcp",
            ),
        )
        assert "/mcp-auth/login?session_id=" in url

    async def test_authorize_rejects_wrong_resource(
        self, provider: PASAuthProvider, registered_client: OAuthClientInformationFull
    ):
        with pytest.raises(AuthorizeError):
            await provider.authorize(
                registered_client,
                AuthorizationParams(
                    state="s",
                    scopes=[],
                    code_challenge="c",
                    redirect_uri="http://localhost/callback",
                    redirect_uri_provided_explicitly=True,
                    resource="http://evil.com/mcp",
                ),
            )

    async def test_authorize_defaults_resource_when_absent(
        self, provider: PASAuthProvider, registered_client: OAuthClientInformationFull
    ):
        url = await provider.authorize(
            registered_client,
            AuthorizationParams(
                state="s",
                scopes=[],
                code_challenge="c",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                resource=None,
            ),
        )
        assert "/mcp-auth/login?session_id=" in url


class TestCodeExchange:
    async def test_load_valid_code(self, provider, session_factory):
        code = "test-auth-code-xyz"
        async with session_factory() as session:
            from server.models.oauth import OAuthAuthorizationCode

            code_record = OAuthAuthorizationCode(
                code_hash=hashlib.sha256(code.encode()).hexdigest(),
                client_id="test-client",
                user_id="user-1",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                code_challenge="challenge",
                code_challenge_method="S256",
                resource="http://localhost:18760/mcp",
                scopes='["openid"]',
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            session.add(code_record)
            await session.commit()

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, code)
        assert loaded is not None
        assert loaded.client_id == "test-client"
        assert loaded.resource == "http://localhost:18760/mcp"
        assert loaded.scopes == ["openid"]
        assert loaded.subject == "user-1"
        assert loaded.code == code
        assert str(loaded.redirect_uri) == "http://localhost/callback"
        assert loaded.redirect_uri_provided_explicitly is True
        assert loaded.code_challenge == "challenge"

    async def test_load_nonexistent_code(self, provider, session_factory):
        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, "no-such-code")
        assert loaded is None

    async def test_consumed_code_returns_none(self, provider, session_factory):
        code = "consumed-code"
        async with session_factory() as session:
            from server.models.oauth import OAuthAuthorizationCode

            code_record = OAuthAuthorizationCode(
                code_hash=hashlib.sha256(code.encode()).hexdigest(),
                client_id="test-client",
                user_id="user-1",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                code_challenge="c",
                code_challenge_method="S256",
                resource="http://localhost:18760/mcp",
                scopes="[]",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                consumed_at=datetime.now(timezone.utc),  # already consumed
            )
            session.add(code_record)
            await session.commit()

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, code)
        assert loaded is None

    async def test_consumed_code_revokes_refresh_tokens(
        self, provider, session_factory
    ):
        code = "replay-code"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        async with session_factory() as session:
            from server.models.oauth import (
                OAuthAuthorizationCode,
                OAuthRefreshToken,
            )

            code_record = OAuthAuthorizationCode(
                code_hash=code_hash,
                client_id="test-client",
                user_id="user-1",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                code_challenge="c",
                code_challenge_method="S256",
                resource="http://localhost:18760/mcp",
                scopes="[]",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                consumed_at=datetime.now(timezone.utc),
            )
            rt = OAuthRefreshToken(
                token_hash="fake-token-hash-abc",
                client_id="test-client",
                user_id="user-1",
                code_id=code_hash,
                token_family="family-1",
                scopes="[]",
                resource="http://localhost:18760/mcp",
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
            session.add(code_record)
            session.add(rt)
            await session.commit()

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, code)
        assert loaded is None

        # Verify refresh token was revoked
        from sqlalchemy import select
        from server.models.oauth import OAuthRefreshToken

        async with session_factory() as session:
            result = await session.execute(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_hash == "fake-token-hash-abc"
                )
            )
            rt_row = result.scalar_one()
            assert rt_row.revoked_at is not None

    async def test_exchange_code_returns_jwt(self, provider, session_factory):
        code = "test-exchange-code"
        async with session_factory() as session:
            from server.models.oauth import OAuthAuthorizationCode

            code_record = OAuthAuthorizationCode(
                code_hash=hashlib.sha256(code.encode()).hexdigest(),
                client_id="test-client",
                user_id="user-1",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                code_challenge="challenge",
                code_challenge_method="S256",
                resource="http://localhost:18760/mcp",
                scopes='["openid"]',
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            session.add(code_record)
            await session.commit()

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, code)
        token = await provider.exchange_authorization_code(client, loaded)
        assert token.access_token
        assert token.refresh_token
        assert token.token_type == "Bearer"
        from server.config import get_config

        cfg_minutes = get_config().auth.jwt.access_token_expire_minutes
        assert token.expires_in == cfg_minutes * 60
        assert token.scope == "openid"

        # Verify JWT contents
        from jose import jwt as jose_jwt
        from server.auth.jwt_manager import get_public_key

        payload = jose_jwt.decode(
            token.access_token,
            get_public_key(),
            algorithms=["RS256"],
            audience="http://localhost:18760/mcp",
        )
        assert payload["aud"] == "http://localhost:18760/mcp"
        assert payload["sub"] == "user-1"
        assert payload["client_id"] == "test-client"
        assert payload["scope"] == "openid"
        assert payload["type"] == "access"
        assert "jti" in payload
        assert "iat" in payload
        assert "exp" in payload

    async def test_exchange_code_marks_consumed(self, provider, session_factory):
        code = "consume-me-code"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        async with session_factory() as session:
            from server.models.oauth import OAuthAuthorizationCode

            code_record = OAuthAuthorizationCode(
                code_hash=code_hash,
                client_id="test-client",
                user_id="user-1",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                code_challenge="challenge",
                code_challenge_method="S256",
                resource="http://localhost:18760/mcp",
                scopes='["openid"]',
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            session.add(code_record)
            await session.commit()

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, code)
        await provider.exchange_authorization_code(client, loaded)

        # Verify code is now consumed
        from sqlalchemy import select
        from server.models.oauth import OAuthAuthorizationCode

        async with session_factory() as session:
            result = await session.execute(
                select(OAuthAuthorizationCode).where(
                    OAuthAuthorizationCode.code_hash == code_hash
                )
            )
            row = result.scalar_one()
            assert row.consumed_at is not None

    async def test_exchange_code_stores_refresh_token(
        self, provider, session_factory
    ):
        code = "refresh-store-code"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        async with session_factory() as session:
            from server.models.oauth import OAuthAuthorizationCode

            code_record = OAuthAuthorizationCode(
                code_hash=code_hash,
                client_id="test-client",
                user_id="user-1",
                redirect_uri="http://localhost/callback",
                redirect_uri_provided_explicitly=True,
                code_challenge="challenge",
                code_challenge_method="S256",
                resource="http://localhost:18760/mcp",
                scopes='["openid"]',
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            session.add(code_record)
            await session.commit()

        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_authorization_code(client, code)
        token = await provider.exchange_authorization_code(client, loaded)

        # Verify refresh token is stored in DB
        from sqlalchemy import select
        from server.models.oauth import OAuthRefreshToken

        rt_hash = hashlib.sha256(token.refresh_token.encode()).hexdigest()
        async with session_factory() as session:
            result = await session.execute(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_hash == rt_hash
                )
            )
            rt_row = result.scalar_one()
            assert rt_row.client_id == "test-client"
            assert rt_row.user_id == "user-1"
            assert rt_row.code_id == code_hash
            assert rt_row.revoked_at is None
            assert rt_row.resource == "http://localhost:18760/mcp"


async def _issue_tokens(provider, session_factory) -> tuple[str, str]:
    """Helper that creates a code in DB, exchanges it, returns (access, refresh)."""
    code = f"code-{uuid.uuid4()}"
    async with session_factory() as session:
        session.add(OAuthAuthorizationCode(
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            client_id="test-client",
            user_id="user-1",
            redirect_uri="http://localhost/callback",
            redirect_uri_provided_explicitly=True,
            code_challenge="c",
            code_challenge_method="S256",
            resource="http://localhost:18760/mcp",
            scopes='["openid"]',
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
        await session.commit()
    client = OAuthClientInformationFull(
        client_id="test-client",
        redirect_uris=["http://localhost/callback"],
    )
    loaded = await provider.load_authorization_code(client, code)
    token = await provider.exchange_authorization_code(client, loaded)
    return token.access_token, token.refresh_token


class TestRefreshToken:
    async def test_load_and_exchange_refresh(self, provider, session_factory):
        access, refresh = await _issue_tokens(provider, session_factory)
        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_refresh_token(client, refresh)
        assert loaded is not None
        assert loaded.client_id == "test-client"
        assert loaded.subject == "user-1"
        new_token = await provider.exchange_refresh_token(client, loaded, [])
        assert new_token.access_token != access
        assert new_token.refresh_token != refresh

    async def test_old_refresh_revoked_after_rotation(
        self, provider, session_factory
    ):
        _, refresh = await _issue_tokens(provider, session_factory)
        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_refresh_token(client, refresh)
        await provider.exchange_refresh_token(client, loaded, [])
        # Old refresh should now be revoked
        loaded_again = await provider.load_refresh_token(client, refresh)
        assert loaded_again is None

    async def test_reuse_detection_revokes_family(
        self, provider, session_factory
    ):
        _, refresh = await _issue_tokens(provider, session_factory)
        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_refresh_token(client, refresh)
        new_token = await provider.exchange_refresh_token(client, loaded, [])
        # Try to use old refresh again -> reuse detection
        reuse_result = await provider.load_refresh_token(client, refresh)
        assert reuse_result is None
        # New token should also be revoked now
        new_result = await provider.load_refresh_token(
            client, new_token.refresh_token
        )
        assert new_result is None


class TestAccessToken:
    async def test_load_valid_access_token(self, provider, session_factory):
        access, _ = await _issue_tokens(provider, session_factory)
        loaded = await provider.load_access_token(access)
        assert loaded is not None
        assert loaded.subject == "user-1"
        assert loaded.resource == "http://localhost:18760/mcp"
        assert loaded.client_id == "test-client"
        assert "openid" in loaded.scopes

    async def test_load_invalid_token(self, provider):
        result = await provider.load_access_token("not-a-jwt")
        assert result is None

    async def test_load_wrong_audience(self, provider):
        from jose import jwt as jose_jwt
        from server.auth.jwt_manager import _load_keys

        private_key, _ = _load_keys()
        bad_token = jose_jwt.encode(
            {
                "sub": "u",
                "aud": "http://evil.com",
                "jti": "j",
                "exp": int(time.time()) + 3600,
                "type": "access",
            },
            private_key,
            algorithm="RS256",
        )
        result = await provider.load_access_token(bad_token)
        assert result is None


class TestRevocation:
    async def test_revoke_access_token(self, provider, session_factory):
        access, _ = await _issue_tokens(provider, session_factory)
        loaded = await provider.load_access_token(access)
        assert loaded is not None
        await provider.revoke_token(loaded)
        # Should now be denied
        result = await provider.load_access_token(access)
        assert result is None

    async def test_revoke_refresh_token(self, provider, session_factory):
        _, refresh = await _issue_tokens(provider, session_factory)
        client = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/callback"],
        )
        loaded = await provider.load_refresh_token(client, refresh)
        assert loaded is not None
        await provider.revoke_token(loaded)
        # Should now be revoked
        result = await provider.load_refresh_token(client, refresh)
        assert result is None
