from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.management.settings import (
    ListenerSettings,
    ManagedIdentitySettings,
)
from server.runtime import RuntimePhase


def _settings() -> ListenerSettings:
    return ListenerSettings(
        business_port=18080,
        management_port=18081,
        auth_mode="trusted-network",
        token=None,
        managed_identity=ManagedIdentitySettings("pmcp-test", 1),
    )


def test_management_listener_contains_only_management_routes() -> None:
    from server.management.app import create_management_app

    runtime = SimpleNamespace(
        phase=RuntimePhase.STARTING,
        error_code=None,
        application_state=None,
    )
    app = create_management_app(runtime, _settings())
    paths = {getattr(route, "path", None) for route in app.routes}

    assert paths == {
        "/healthz",
        "/api/internal/v1/status",
        "/api/internal/v1/schema",
        "/api/internal/v1/features/knowledge",
        "/api/internal/v1/config",
        "/api/internal/v1/accounts",
        "/api/internal/v1/accounts/admin/password",
    }
    client = TestClient(app)
    for path in (
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/instances",
        "/auth/login",
        "/mcp",
    ):
        assert client.get(path).status_code == 404, path


def test_business_listener_has_no_internal_management_routes() -> None:
    from server.app import create_app

    runtime = SimpleNamespace()
    app = create_app(runtime=runtime)

    assert not any(
        getattr(route, "path", "").startswith("/api/internal/v1")
        for route in app.routes
    )
