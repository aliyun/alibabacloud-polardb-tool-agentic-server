from __future__ import annotations

import base64

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from server.app import create_app
from server.config import reset_config
from server.mcp.transport import reset_mcp


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("PAS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
    # Exercise real SPA fallback even when frontend artifacts are absent.
    (tmp_path / "index.html").write_text("<html>SPA sentinel</html>")
    (tmp_path / "assets").mkdir()
    monkeypatch.setenv("PAS_STATIC_DIR", str(tmp_path))
    reset_config()
    reset_mcp()
    yield create_app()
    reset_mcp()
    reset_config()


@pytest.mark.parametrize("path", [
    "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
    "/api/openapi.json", "/mcp/rest/docs", "/mcp/rest/openapi.json",
])
@pytest.mark.parametrize("accept", ["application/json", "text/html"])
async def test_documentation_is_not_served_or_spa_redirected(app, path, accept):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method in ("GET", "HEAD"):
            for suffix in ("", "/"):
                response = await client.request(method, path + suffix, headers={"Accept": accept})
                assert response.status_code == 404
                assert "SPA sentinel" not in response.text
                assert "SwaggerUIBundle" not in response.text


@pytest.mark.parametrize("path", [
    "/mcp/rest/tools", "/help/enterprise-identity-sources",
    "/help/enterprise-identity-sources-api",
])
async def test_discovery_requires_authentication(app, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
        assert response.status_code == 401


@pytest.mark.parametrize("role,status", [("member", 403), ("admin", 200)])
async def test_help_requires_admin_but_tools_allow_members(app, role, status):
    from server.auth.dependencies import get_current_user
    from server.models import User, UserRole

    app.dependency_overrides[get_current_user] = lambda: User(role=UserRole(role))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/help/enterprise-identity-sources", "/help/enterprise-identity-sources-api"):
            assert (await client.get(path)).status_code == status
        response = await client.get("/mcp/rest/tools")
        assert response.status_code == 200
        assert len(response.json()["tools"]) == 7


def test_all_business_routes_have_authentication_or_explicit_public_contract(app):
    from server.api.router import build_knowledge_router

    public = {
        ("GET", path) for path in (
            "/auth/web-sso-guard/callback", "/livez", "/readyz", "/healthz/dependencies",
            "/auth/oidc/login", "/auth/oidc/callback", "/auth/feishu/login",
            "/auth/feishu/login/callback", "/auth/sharepoint/login",
            "/auth/sharepoint/login/callback", "/auth/feishu/tenant-verification/callback",
            "/auth/mode", "/favicon.ico", "/auth/dingtalk/login", "/auth/dingtalk/callback",
        )
    } | {
        ("POST", path) for path in (
            "/auth/login", "/auth/recovery/login", "/auth/refresh", "/auth/logout",
            "/api/v1/external-auth/token",
        )
    }
    gates = {"get_current_user", "get_current_agent", "resolve_config_actor", "get_platform_access_context"}

    def dependencies(node):
        yield getattr(node.call, "__name__", "")
        for child in node.dependencies:
            yield from dependencies(child)

    for route in [*app.routes, *build_knowledge_router().routes]:
        if isinstance(route, APIRoute) and not gates.intersection(dependencies(route.dependant)):
            for method in route.methods:
                assert (method, route.path) in public, f"Unclassified anonymous route: {method} {route.path}"
