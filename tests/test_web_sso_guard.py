"""Tests for Web SSO Guard."""
from __future__ import annotations

import os
import time
import base64
from unittest.mock import patch, AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa
from httpx import ASGITransport
from jose import jwt as jose_jwt
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from server.config import AppConfig, reset_config
from server.db.engine import reset_engine
from server.mcp.transport import reset_mcp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv(
        "PAS_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
    )
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(
            b"01234567890123456789012345678901"
        ).decode(),
    )
    reset_config()
    yield
    reset_config()


def _generate_test_keypair() -> tuple[str, str]:
    key = crypto_rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


_TEST_PRIVATE, _TEST_PUBLIC = _generate_test_keypair()


def _sign_guard_cookie(private_key: str = _TEST_PRIVATE, ttl_hours: int = 8) -> str:
    now = int(time.time())
    return jose_jwt.encode(
        {"type": "web_sso_guard", "iat": now, "exp": now + ttl_hours * 3600},
        private_key,
        algorithm="RS256",
    )


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

def test_web_sso_guard_config_defaults():
    os.environ.pop("PAS_AUTH_WEB_SSO_GUARD_ENABLED", None)
    os.environ.pop("PAS_AUTH_WEB_SSO_GUARD_SESSION_TTL_HOURS", None)
    config = AppConfig()
    assert config.auth.web_sso_guard.enabled is False
    assert config.auth.web_sso_guard.session_ttl_hours == 8
    assert config.auth.web_sso_guard.excluded_paths == []


def test_web_sso_guard_config_is_not_loaded_from_env(monkeypatch):
    monkeypatch.setenv("PAS_AUTH_WEB_SSO_GUARD_ENABLED", "true")
    monkeypatch.setenv("PAS_AUTH_WEB_SSO_GUARD_SESSION_TTL_HOURS", "12")
    config = AppConfig()
    assert config.auth.web_sso_guard.enabled is False
    assert config.auth.web_sso_guard.session_ttl_hours == 8


# ---------------------------------------------------------------------------
# Middleware tests
# ---------------------------------------------------------------------------

def _make_app_with_guard(extra_excluded: list[str] | None = None):
    from server.auth.web_sso_guard import WebSSOGuardMiddleware

    async def homepage(request):
        return PlainTextResponse("OK")

    app = Starlette(routes=[
        Route("/", homepage),
        Route("/dashboard", homepage),
        Route("/livez", homepage),
        Route("/readyz", homepage),
        Route("/healthz/dependencies", homepage),
        Route("/mcp", homepage),
        Route("/mcp/test", homepage),
        Route("/.well-known/openid-configuration", homepage),
        Route("/mcp-auth/login", homepage),
        Route("/auth/oidc/callback", homepage),
        Route("/auth/web-sso-guard/callback", homepage),
    ])
    app.add_middleware(
        WebSSOGuardMiddleware,
        config={"excluded_paths": extra_excluded or []},
    )
    return app


class TestMiddlewareExclusions:
    def test_excluded_paths_pass_through(self):
        with patch("server.auth.web_sso_guard._load_keys", return_value=(_TEST_PRIVATE, _TEST_PUBLIC)):
            app = _make_app_with_guard()
            client = TestClient(app, follow_redirects=False)

            for path in [
                "/livez", "/readyz", "/healthz/dependencies",
                "/mcp", "/mcp/test",
                "/.well-known/openid-configuration",
                "/mcp-auth/login",
                "/auth/oidc/callback",
                "/auth/web-sso-guard/callback",
            ]:
                resp = client.get(path)
                assert resp.status_code == 200, f"Expected 200 for {path}, got {resp.status_code}"

    def test_custom_excluded_paths(self):
        with patch("server.auth.web_sso_guard._load_keys", return_value=(_TEST_PRIVATE, _TEST_PUBLIC)):
            app = _make_app_with_guard(extra_excluded=["/custom-webhook"])

            async def custom(request):
                return PlainTextResponse("custom")

            app.routes.append(Route("/custom-webhook", custom))
            client = TestClient(app, follow_redirects=False)
            resp = client.get("/custom-webhook")
            assert resp.status_code == 200


def _mock_sso_redirect_context():
    """Context manager that mocks SSO redirect dependencies."""
    mock_federation = MagicMock()
    mock_federation.discover_endpoints = AsyncMock()
    mock_federation.build_authorize_url.return_value = (
        "https://login.example.com/oauth2/auth?client_id=test&state=xxx",
        None,
    )

    mock_cfg = type("C", (), {
        "auth": type("A", (), {
            "oidc": type("O", (), {
                "provider_name": "generic-oidc",
                "client_id": "test",
                "client_secret": "",
                "scopes": ["openid"],
                "idp_pkce": False,
            })(),
        })(),
        "server": type("S", (), {"public_base_url": "http://localhost:18760"})(),
    })()

    from contextlib import contextmanager

    @contextmanager
    def ctx():
        with (
            patch("server.auth.web_sso_guard._load_keys", return_value=(_TEST_PRIVATE, _TEST_PUBLIC)),
            patch("server.config.get_config", return_value=mock_cfg),
            patch("server.auth.identity_federation.IdentityFederation", return_value=mock_federation),
        ):
            yield mock_federation

    return ctx()


class TestMiddlewareRedirect:
    def test_protected_path_redirects_without_cookie(self):
        with _mock_sso_redirect_context():
            app = _make_app_with_guard()
            client = TestClient(app, follow_redirects=False)

            resp = client.get("/dashboard")
            assert resp.status_code == 302
            assert "login.example.com" in resp.headers["location"]


class TestMiddlewareCookieValidation:
    def test_valid_cookie_passes_through(self):
        with patch("server.auth.web_sso_guard._load_keys", return_value=(_TEST_PRIVATE, _TEST_PUBLIC)):
            app = _make_app_with_guard()
            client = TestClient(app, follow_redirects=False)

            token = _sign_guard_cookie()
            client.cookies.set("web_sso_guard", token)
            resp = client.get("/dashboard")
            assert resp.status_code == 200

    def test_expired_cookie_redirects(self):
        with _mock_sso_redirect_context():
            app = _make_app_with_guard()
            client = TestClient(app, follow_redirects=False)

            now = int(time.time())
            expired_payload = {"type": "web_sso_guard", "iat": now - 7200, "exp": now - 3600}
            token = jose_jwt.encode(expired_payload, _TEST_PRIVATE, algorithm="RS256")
            client.cookies.set("web_sso_guard", token)
            resp = client.get("/dashboard")
            assert resp.status_code == 302

    def test_wrong_type_cookie_redirects(self):
        with _mock_sso_redirect_context():
            app = _make_app_with_guard()
            client = TestClient(app, follow_redirects=False)

            now = int(time.time())
            wrong_payload = {"type": "access", "iat": now, "exp": now + 3600}
            token = jose_jwt.encode(wrong_payload, _TEST_PRIVATE, algorithm="RS256")
            client.cookies.set("web_sso_guard", token)
            resp = client.get("/dashboard")
            assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Callback helper tests
# ---------------------------------------------------------------------------

class TestCallbackHelpers:
    def test_decode_state_roundtrip(self):
        from server.auth.web_sso_guard import _build_state, decode_state
        state = _build_state("/dashboard")
        decoded = decode_state(state)
        assert decoded["path"] == "/dashboard"

    def test_decode_state_with_query(self):
        from server.auth.web_sso_guard import _build_state, decode_state
        state = _build_state("/users?page=2&sort=name")
        decoded = decode_state(state)
        assert decoded["path"] == "/users?page=2&sort=name"

    def test_decode_state_invalid_returns_root(self):
        from server.auth.web_sso_guard import decode_state
        decoded = decode_state("not-valid-base64!!!")
        assert decoded["path"] == "/"

    def test_validate_redirect_path_rejects_absolute_urls(self):
        from server.auth.web_sso_guard import validate_redirect_path
        assert validate_redirect_path("https://evil.com") == "/"
        assert validate_redirect_path("//evil.com") == "/"
        assert validate_redirect_path("") == "/"
        assert validate_redirect_path("javascript:alert(1)") == "/"

    def test_validate_redirect_path_allows_relative(self):
        from server.auth.web_sso_guard import validate_redirect_path
        assert validate_redirect_path("/dashboard") == "/dashboard"
        assert validate_redirect_path("/users?page=2") == "/users?page=2"
        assert validate_redirect_path("/") == "/"


# ---------------------------------------------------------------------------
# Callback route tests
# ---------------------------------------------------------------------------

class TestAppIntegration:
    def test_runtime_policy_is_always_registered(self):
        reset_config()

        from server.app import create_app
        app = create_app()

        from server.middleware.runtime_policy import (
            RuntimePolicyMiddleware,
        )
        has_runtime_policy = any(
            m.cls is RuntimePolicyMiddleware
            for m in getattr(app, "user_middleware", [])
        )
        assert has_runtime_policy

    def test_environment_cannot_enable_sso(self, monkeypatch):
        monkeypatch.setenv("PAS_AUTH_MODE", "oidc")
        monkeypatch.setenv("PAS_AUTH_WEB_SSO_GUARD_ENABLED", "true")
        reset_config()

        from server.app import create_app
        app = create_app()

        from server.auth.web_sso_guard import WebSSOGuardMiddleware
        has_guard = any(
            m.cls is WebSSOGuardMiddleware
            for m in getattr(app, "user_middleware", [])
        )
        assert not has_guard
        assert app.state.runtime_access_policy.sso_active is False


class TestWebSSOGuardCallback:
    def test_callback_missing_params(self):
        from server.auth.web_sso_guard import handle_web_sso_guard_callback

        app = Starlette(routes=[
            Route("/auth/web-sso-guard/callback", handle_web_sso_guard_callback),
        ])
        client = TestClient(app)
        resp = client.get("/auth/web-sso-guard/callback")
        assert resp.status_code == 400

    def test_callback_missing_code(self):
        from server.auth.web_sso_guard import handle_web_sso_guard_callback, _build_state

        app = Starlette(routes=[
            Route("/auth/web-sso-guard/callback", handle_web_sso_guard_callback),
        ])
        client = TestClient(app)
        state = _build_state("/dashboard")
        resp = client.get(f"/auth/web-sso-guard/callback?state={state}")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# E2E tests with httpx AsyncClient
# ---------------------------------------------------------------------------


class TestE2EGuardFlow:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setenv("PAS_SERVER_DEV_MODE", "true")
        monkeypatch.setenv("PAS_AUTH_MODE", "oidc")
        monkeypatch.setenv("PAS_OIDC_AUTHORIZATION_ENDPOINT", "https://login.example.com/oauth2/auth")
        monkeypatch.setenv("PAS_OIDC_TOKEN_ENDPOINT", "https://login.example.com/oauth2/token")
        monkeypatch.setenv("PAS_OIDC_CLIENT_ID", "test-client")
        monkeypatch.setenv("PAS_AUTH_WEB_SSO_GUARD_ENABLED", "true")
        monkeypatch.setenv("PAS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        reset_config()
        reset_mcp()
        reset_engine()
        yield
        reset_config()
        reset_mcp()
        reset_engine()

    @pytest.mark.asyncio
    async def test_unprotected_paths_bypass_guard(self):
        from server.app import create_app
        app = create_app()

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/livez")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_environment_only_sso_is_not_activated(self):
        from server.app import create_app
        app = create_app()

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.get("/auth/me")
            assert resp.status_code == 401
