from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.external_tokens import (
    ExternalTokenAuthenticator,
    ExternalTokenIdentity,
    ExternalTokenInvalid,
)
from server.config import AppConfig
from server.core.crypto import encrypt
from server.models import (
    AuthProvider,
    Base,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    User,
    UserExternalIdentity,
    UserRole,
    UserWorkspace,
)


@pytest.fixture
def encryption_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", key)
    return key


@pytest.fixture
async def session_factory(encryption_key):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _config(provider: str) -> AppConfig:
    config = AppConfig()
    trust = config.auth.external_token_trust
    trust.enabled = True
    trust.provider = provider
    trust.config_digest = "external-token-test"
    config.auth.oidc.provider_name = "test-idp"
    config.auth.oidc.client_id = "client-id"
    config.auth.oidc.client_secret = "client-secret"
    config.auth.oidc.issuer = "https://idp.example.com"
    config.auth.oidc.authorization_endpoint = (
        "https://idp.example.com/authorize"
    )
    config.auth.oidc.token_endpoint = "https://idp.example.com/token"
    config.auth.oidc.userinfo_endpoint = (
        "https://idp.example.com/userinfo"
    )
    config.auth.oidc.jwks_uri = "https://idp.example.com/jwks"
    return config


async def test_validates_rfc9068_jwt_access_token(
    session_factory,
    monkeypatch,
):
    config = _config("oidc_jwt")
    config.auth.external_token_trust.expected_audience = "pas-client"
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key()
    ))
    jwk["kid"] = "signing-key"
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(method, url, **_kwargs):
        assert (method, url) == ("GET", "https://idp.example.com/jwks")
        return {"keys": [jwk]}

    monkeypatch.setattr(authenticator, "_json_request", request)
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "sub": "user-123",
            "aud": "pas-client",
            "client_id": "client-id",
            "iat": now,
            "exp": now + 300,
            "jti": "jwt-id",
            "scope": "mcp profile",
            "name": "Alice",
            "email": "alice@example.com",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "signing-key", "typ": "at+jwt"},
    )

    identity = await authenticator.validate(token)

    assert identity.provider_type == "oidc_jwt"
    assert identity.subject == "user-123"
    assert identity.scopes == ("mcp", "profile")
    assert identity.expires_at is not None


async def test_validates_rfc7662_introspection_response(
    session_factory,
    monkeypatch,
):
    config = _config("oauth2_introspection")
    trust = config.auth.external_token_trust
    trust.introspection_endpoint = "https://idp.example.com/introspect"
    trust.expected_audience = "pas-mcp"
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(method, url, **kwargs):
        assert (method, url) == (
            "POST",
            "https://idp.example.com/introspect",
        )
        assert kwargs["data"] == {
            "token": "external-token",
            "token_type_hint": "access_token",
        }
        assert kwargs["headers"]["Authorization"].startswith("Basic ")
        return {
            "active": True,
            "sub": "user-123",
            "aud": ["pas-mcp"],
            "scope": "mcp",
            "exp": int(time.time()) + 300,
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    identity = await authenticator.validate("external-token")

    assert identity.provider_type == "oauth2_introspection"
    assert identity.subject == "user-123"
    assert identity.scopes == ("mcp",)


@pytest.mark.parametrize("source_status", ["active", "stale"])
async def test_introspection_userinfo_uses_existing_identity_mapping(
    session_factory,
    monkeypatch,
    source_status,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="External directory",
            provider=IdentitySourceProvider.SHAREPOINT,
            tenant_id="tenant-external",
        )
        source.status = EnterpriseIdentitySourceStatus(source_status)
        source.last_synced_at = datetime.now(UTC) - timedelta(seconds=30)
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("oauth2_introspection")
    trust = config.auth.external_token_trust
    trust.introspection_endpoint = "https://provider.example.com/introspect"
    trust.userinfo_endpoint = "https://provider.example.com/user_info"
    trust.identity_source_id = source_id
    trust.client_id = "pas-to-provider"
    trust.client_secret = "pas-to-provider-secret"
    trust.expected_audience = "provider-to-pas"
    authenticator = ExternalTokenAuthenticator(session_factory, config)
    requests = []

    async def request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        if url.endswith("/introspect"):
            credentials = base64.b64decode(
                kwargs["headers"]["Authorization"].removeprefix("Basic ")
            ).decode()
            assert credentials == "pas-to-provider:pas-to-provider-secret"
            return {
                "active": True,
                "sub": "provider-user-123",
                "aud": ["provider-to-pas"],
                "scope": "provider.profile",
                "exp": int(time.time()) + 3600,
            }
        assert kwargs["headers"] == {
            "Authorization": "Bearer external-assertion"
        }
        return {
            "sub": "provider-user-123",
            "user_id": "ou_external",
            "union_id": "on_external",
            "pas_source_id": source_id,
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    identity = await authenticator.validate("external-assertion")

    assert [item[:2] for item in requests] == [
        ("POST", "https://provider.example.com/introspect"),
        ("GET", "https://provider.example.com/user_info"),
    ]
    assert identity.provider_type == "oauth2_introspection_userinfo"
    assert identity.provider_key == "sharepoint:tenant-external"
    assert identity.subject == "ou_external"
    assert identity.scopes == ("provider.profile",)
    assert identity.scope_authoritative is False
    assert identity.requires_existing_mapping is True


async def test_introspection_userinfo_rejects_subject_mismatch(
    session_factory,
    monkeypatch,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="External Feishu tenant",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-external",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.config_ciphertext = encrypt(json.dumps({
            "app_id": "cli_external",
            "app_secret": "secret",
        }))
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("oauth2_introspection")
    trust = config.auth.external_token_trust
    trust.introspection_endpoint = "https://provider.example.com/introspect"
    trust.userinfo_endpoint = "https://provider.example.com/user_info"
    trust.identity_source_id = source_id
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(_method, url, **_kwargs):
        if url.endswith("/introspect"):
            return {
                "active": True,
                "sub": "introspection-user",
            }
        return {
            "sub": "different-user",
            "user_id": "ou_external",
            "pas_source_id": source_id,
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    with pytest.raises(ExternalTokenInvalid, match="does not match"):
        await authenticator.validate("external-assertion")


async def test_introspection_userinfo_user_must_already_be_mapped(
    session_factory,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="External Feishu tenant",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-external",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.config_ciphertext = encrypt(json.dumps({
            "app_id": "cli_external",
            "app_secret": "secret",
        }))
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("oauth2_introspection")
    config.auth.external_token_trust.identity_source_id = source_id
    authenticator = ExternalTokenAuthenticator(session_factory, config)
    identity = ExternalTokenIdentity(
        provider_type="oauth2_introspection_userinfo",
        provider_key="feishu:tenant-external",
        provider_fingerprint="fingerprint",
        subject="ou_external",
        display_name=None,
        email=None,
        scopes=("provider.profile",),
        expires_at=None,
        scope_authoritative=False,
        requires_existing_mapping=True,
    )

    async with session_factory() as session:
        with pytest.raises(
            ExternalTokenInvalid,
            match="not mapped",
        ):
            await authenticator.resolve_user(session, identity)

        user = User(
            external_id="feishu:tenant-external:ou_external",
            display_name="External User",
            auth_provider=AuthProvider.OIDC,
            role=UserRole.MEMBER,
        )
        session.add(user)
        await session.flush()
        session.add(
            UserExternalIdentity(
                user_id=user.id,
                identity_provider="feishu:tenant-external",
                external_subject="ou_external",
            )
        )
        await session.flush()

        resolved = await authenticator.resolve_user(session, identity)

    assert resolved.id == user.id


async def test_resolve_user_recovers_concurrent_identity_and_workspace(
    session_factory,
    monkeypatch,
) -> None:
    provider_key = "oidc:https://idp.example.test"
    subject = "concurrent-user"
    async with session_factory() as session:
        recovered_user = User(
            external_id=f"{provider_key}:{subject}",
            display_name="Concurrent user",
            auth_provider=AuthProvider.OIDC,
            role=UserRole.MEMBER,
        )
        session.add(recovered_user)
        await session.flush()
        session.add(
            UserExternalIdentity(
                user_id=recovered_user.id,
                identity_provider=provider_key,
                external_subject=subject,
            )
        )
        await session.commit()
        recovered_user_id = recovered_user.id

        real_scalar = session.scalar
        scalar_calls = 0

        async def miss_identity_before_concurrent_insert(*args, **kwargs):
            nonlocal scalar_calls
            scalar_calls += 1
            if scalar_calls == 1:
                return None
            return await real_scalar(*args, **kwargs)

        monkeypatch.setattr(session, "scalar", miss_identity_before_concurrent_insert)
        authenticator = ExternalTokenAuthenticator(
            session_factory, _config("oauth2_userinfo")
        )
        identity = ExternalTokenIdentity(
            provider_type="oauth2_userinfo",
            provider_key=provider_key,
            provider_fingerprint="fingerprint",
            subject=subject,
            display_name="Concurrent user",
            email=None,
            scopes=(),
            expires_at=None,
        )

        resolved = await authenticator.resolve_user(session, identity)

        assert resolved.id == recovered_user_id
        workspace = await session.scalar(
            select(UserWorkspace).where(UserWorkspace.user_id == recovered_user_id)
        )
        assert workspace is not None


@pytest.mark.parametrize(
    ("subject_claim", "subject_value"),
    [("account_id", 123456), ("accountId", 654321)],
)
async def test_validates_buc_form_userinfo_and_client_binding(
    session_factory,
    monkeypatch,
    subject_claim,
    subject_value,
):
    config = _config("buc")
    config.auth.oidc.protocol_mode = "oauth2_userinfo"
    config.auth.oidc.user_id_claim = "account_id"
    config.auth.oidc.userinfo_token_method = "form_post"
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(method, url, **kwargs):
        assert (method, url) == (
            "POST",
            "https://idp.example.com/userinfo",
        )
        assert kwargs["data"] == {"access_token": "buc-token"}
        return {
            subject_claim: subject_value,
            "client_id": "client-id",
            "name": "BUC User",
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    identity = await authenticator.validate("buc-token")

    assert identity.provider_type == "buc"
    assert identity.subject == str(subject_value)


async def test_rejects_buc_token_issued_to_another_client(
    session_factory,
    monkeypatch,
):
    config = _config("buc")
    config.auth.oidc.protocol_mode = "oauth2_userinfo"
    config.auth.oidc.user_id_claim = "account_id"
    config.auth.oidc.userinfo_token_method = "form_post"
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(_method, _url, **_kwargs):
        return {
            "account_id": 123456,
            "client_id": "another-client",
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    with pytest.raises(
        ExternalTokenInvalid,
        match="client ID is invalid",
    ):
        await authenticator.validate("buc-token")


async def test_validates_userinfo_without_browser_oidc_discovery(
    session_factory,
    monkeypatch,
):
    config = _config("oauth2_userinfo")
    config.auth.oidc.discovery_url = None
    config.auth.oidc.issuer = None
    config.auth.oidc.authorization_endpoint = None
    config.auth.oidc.token_endpoint = None
    config.auth.oidc.userinfo_endpoint = None
    config.auth.oidc.jwks_uri = None
    config.auth.external_token_trust.userinfo_endpoint = (
        "https://provider.example.com/userinfo"
    )
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(method, url, **kwargs):
        assert (method, url) == (
            "GET",
            "https://provider.example.com/userinfo",
        )
        assert kwargs["headers"] == {
            "Authorization": "Bearer provider-token"
        }
        return {"sub": "external-user", "name": "External User"}

    monkeypatch.setattr(authenticator, "_json_request", request)

    identity = await authenticator.validate("provider-token")

    assert identity.provider_type == "oauth2_userinfo"
    assert identity.subject == "external-user"


async def test_validates_feishu_user_token_against_verified_tenant(
    session_factory,
    monkeypatch,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Feishu tenant",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-001",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.config_ciphertext = encrypt(json.dumps({
            "app_id": "cli_test",
            "app_secret": "secret",
        }))
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("feishu")
    config.auth.external_token_trust.identity_source_id = source_id
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(method, _url, **kwargs):
        assert method == "GET"
        assert kwargs["headers"] == {
            "Authorization": "Bearer feishu-token"
        }
        return {
            "code": 0,
            "data": {
                "tenant_key": "tenant-001",
                "user_id": "ou_123",
                "name": "Feishu User",
                "email": "user@example.com",
            },
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    identity = await authenticator.validate("feishu-token")

    assert identity.provider_type == "feishu"
    assert identity.subject == "ou_123"
    assert identity.provider_key == "feishu:tenant-001"


@pytest.mark.parametrize("source_status", ["active", "stale"])
async def test_validates_feishu_exchange_assertion_with_direct_identity_context(
    session_factory,
    monkeypatch,
    source_status,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Feishu tenant",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-001",
        )
        source.status = EnterpriseIdentitySourceStatus(source_status)
        source.last_synced_at = datetime.now(UTC) - timedelta(seconds=30)
        source.config_ciphertext = encrypt(json.dumps({
            "app_id": "cli_test",
            "app_secret": "secret",
        }))
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("feishu")
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(method, _url, **kwargs):
        assert method == "GET"
        assert kwargs["headers"] == {
            "Authorization": "Bearer feishu-token"
        }
        return {
            "code": 0,
            "data": {
                "tenant_key": "tenant-001",
                "user_id": "ou_123",
                "union_id": "on_123",
            },
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    identity = await authenticator.validate(
        "feishu-token",
        identity_source_id=source_id,
        feishu_user_id="ou_123",
        feishu_union_id="on_123",
    )

    assert identity.provider_type == "feishu"
    assert identity.subject == "ou_123"
    assert identity.provider_key == "feishu:tenant-001"

    with pytest.raises(
        ExternalTokenInvalid,
        match="identity context is incomplete",
    ):
        await authenticator.validate(
            "feishu-token",
            identity_source_id=source_id,
            feishu_user_id="ou_123",
        )

    with pytest.raises(
        ExternalTokenInvalid,
        match="user ID does not match",
    ):
        await authenticator.validate(
            "feishu-token",
            identity_source_id=source_id,
            feishu_user_id="ou_other",
            feishu_union_id="on_123",
        )

    with pytest.raises(
        ExternalTokenInvalid,
        match="union ID does not match",
    ):
        await authenticator.validate(
            "feishu-token",
            identity_source_id=source_id,
            feishu_user_id="ou_123",
            feishu_union_id="on_other",
        )


@pytest.mark.parametrize("source_status", ["active", "stale"])
async def test_resolves_feishu_user_from_dynamic_identity_source(
    session_factory,
    monkeypatch,
    source_status,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Dynamic Feishu tenant",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-dynamic",
        )
        source.status = EnterpriseIdentitySourceStatus(source_status)
        source.last_synced_at = datetime.now(UTC) - timedelta(seconds=30)
        source.config_ciphertext = encrypt(json.dumps({
            "app_id": "dynamic_test",
            "app_secret": "secret",
        }))
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("feishu")
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(_method, _url, **_kwargs):
        return {
            "code": 0,
            "data": {
                "tenant_key": "tenant-dynamic",
                "user_id": "ou_dynamic",
                "union_id": "on_dynamic",
            },
        }

    monkeypatch.setattr(authenticator, "_json_request", request)
    identity = await authenticator.validate(
        "feishu-token",
        identity_source_id=source_id,
        feishu_user_id="ou_dynamic",
        feishu_union_id="on_dynamic",
    )

    async with session_factory() as session:
        user = await authenticator.resolve_user(session, identity)

    assert user.external_id == "feishu:tenant-dynamic:ou_dynamic"


async def test_rejects_feishu_user_token_from_another_tenant(
    session_factory,
    monkeypatch,
):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Feishu tenant",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-001",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.config_ciphertext = encrypt(json.dumps({
            "app_id": "cli_test",
            "app_secret": "secret",
        }))
        session.add(source)
        await session.commit()
        source_id = source.id

    config = _config("feishu")
    config.auth.external_token_trust.identity_source_id = source_id
    authenticator = ExternalTokenAuthenticator(session_factory, config)

    async def request(_method, _url, **_kwargs):
        return {
            "code": 0,
            "data": {
                "tenant_key": "tenant-002",
                "user_id": "ou_123",
            },
        }

    monkeypatch.setattr(authenticator, "_json_request", request)

    with pytest.raises(
        ExternalTokenInvalid,
        match="tenant is not trusted",
    ):
        await authenticator.validate("feishu-token")


@pytest.mark.parametrize("auth_method", ["client_secret_basic", "client_secret_post"])
async def test_http_introspection_uses_dedicated_credentials_without_browser_sso(
    session_factory, monkeypatch, auth_method,
):
    config = AppConfig()
    trust = config.auth.external_token_trust
    trust.enabled = True
    trust.provider = "oauth2_introspection"
    trust.introspection_endpoint = "http://provider.example.com/introspect"
    trust.client_id = "validation-client"
    trust.client_secret = "validation-secret"
    trust.introspection_auth_method = auth_method
    def respond(request):
        assert str(request.url) == trust.introspection_endpoint
        assert request.method == "POST"
        body = parse_qs(request.content.decode())
        assert body["token"] == ["test-external-token"]
        if auth_method == "client_secret_basic":
            credentials = base64.b64encode(b"validation-client:validation-secret").decode()
            assert request.headers["Authorization"] == f"Basic {credentials}"
        else:
            assert body["client_id"] == ["validation-client"]
            assert body["client_secret"] == ["validation-secret"]
        return httpx.Response(200, json={"active": True, "sub": "external-user"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    identity = await ExternalTokenAuthenticator(session_factory, config).validate("test-external-token")
    assert identity.subject == "external-user"
    assert identity.provider_type == "oauth2_introspection"


async def test_http_userinfo_uses_external_endpoint_without_browser_sso(session_factory, monkeypatch):
    config = AppConfig()
    trust = config.auth.external_token_trust
    trust.enabled = True
    trust.provider = "oauth2_userinfo"
    trust.userinfo_endpoint = "http://provider.example.com/userinfo"

    def respond(request):
        assert str(request.url) == trust.userinfo_endpoint
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-external-token"
        return httpx.Response(200, json={"sub": "external-user"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    identity = await ExternalTokenAuthenticator(session_factory, config).validate("test-external-token")
    assert identity.subject == "external-user"
    assert identity.provider_type == "oauth2_userinfo"


@pytest.mark.parametrize("provider", ["feishu", "oauth2_introspection"])
@pytest.mark.parametrize(("status", "age"), [
    ("stale", None), ("stale", 7200), ("disabled", 0), ("pending_binding", 0),
])
async def test_unusable_identity_source_rejected_before_userinfo(session_factory, monkeypatch, provider, status, age):
    async with session_factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Unavailable source", provider=IdentitySourceProvider.FEISHU, tenant_id="tenant-invalid",
        )
        source.status = EnterpriseIdentitySourceStatus(status)
        source.stale_after_seconds = 3600
        source.last_synced_at = None if age is None else datetime.now(UTC) - timedelta(seconds=age)
        source.config_ciphertext = encrypt(json.dumps({"app_id": "test-app", "app_secret": "test-secret"}))
        session.add(source)
        await session.commit()
        source_id = source.id
    config = _config(provider)
    trust = config.auth.external_token_trust
    trust.identity_source_id = source_id
    trust.introspection_endpoint = "https://provider.example.com/introspect"
    trust.userinfo_endpoint = "https://provider.example.com/user_info"
    trust.client_id = "test-client"
    trust.client_secret = "test-secret"
    authenticator = ExternalTokenAuthenticator(session_factory, config)
    requests = []

    async def request(_method, url, **_kwargs):
        requests.append(url)
        assert url.endswith("/introspect")
        return {"active": True, "sub": "test-user", "exp": int(time.time()) + 300}

    monkeypatch.setattr(authenticator, "_json_request", request)
    with pytest.raises(ExternalTokenInvalid, match="Identity source is unavailable"):
        context = ({"identity_source_id": source_id, "feishu_user_id": "test-user",
                    "feishu_union_id": "test-union"} if provider == "feishu" else {})
        await authenticator.validate("test-token", **context)
    assert requests == ([] if provider == "feishu" else [trust.introspection_endpoint])
