
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.app import create_app, provisioning_runtime_lifespan
from server.config import TenantProvisioningConfig, reset_config
from server.mcp.transport import reset_mcp


@pytest.fixture(autouse=True)
def clean(monkeypatch):
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
    reset_mcp()
    yield
    reset_config()
    reset_mcp()


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealthEndpoints:
    async def test_livez(self, client):
        resp = await client.get("/livez")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readyz(self, client):
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_healthz_dependencies(self, client):
        resp = await client.get("/healthz/dependencies")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "dependencies" in data

    async def test_readyz_rejects_traffic_while_local_config_is_stale(
        self, app, client
    ):
        repository = SimpleNamespace(
            global_version=AsyncMock(return_value=7)
        )
        app.state.runtime_config_store = SimpleNamespace(
            repository=repository,
            config_version=6,
            last_error_code=None,
            local_errors={},
        )

        resp = await client.get("/readyz")

        assert resp.status_code == 503
        assert resp.json() == {
            "status": "not_ready",
            "mode": "READY",
            "desired_config_version": 7,
            "loaded_config_version": 6,
            "config_status": "STALE",
            "last_reload_error": None,
            "module_errors": {},
        }

    async def test_readyz_reports_loaded_config_version(
        self, app, client
    ):
        repository = SimpleNamespace(
            global_version=AsyncMock(return_value=7)
        )
        app.state.runtime_config_store = SimpleNamespace(
            repository=repository,
            config_version=7,
            last_error_code=None,
            local_errors={},
        )

        resp = await client.get("/readyz")

        assert resp.status_code == 200
        assert resp.json()["config_status"] == "CURRENT"
        assert resp.json()["desired_config_version"] == 7
        assert resp.json()["loaded_config_version"] == 7


class TestRequestID:
    async def test_response_has_request_id(self, client):
        resp = await client.get("/livez")
        assert "x-request-id" in resp.headers

    async def test_custom_request_id_preserved(self, client):
        resp = await client.get("/livez", headers={"X-Request-ID": "test-123"})
        assert resp.headers["x-request-id"] == "test-123"

    @pytest.mark.parametrize(
        "invalid_request_id",
        [
            "x" * 129,
            "contains spaces",
        ],
    )
    async def test_invalid_request_id_is_replaced_with_safe_opaque_id(
        self, client, invalid_request_id
    ):
        resp = await client.get(
            "/livez",
            headers={"X-Request-ID": invalid_request_id},
        )
        request_id = resp.headers["x-request-id"]
        assert request_id != invalid_request_id
        assert 1 <= len(request_id) <= 128
        assert request_id.isascii()
        assert all(
            character.isalnum() or character in "._:-"
            for character in request_id
        )


async def test_provisioning_lifespan_starts_global_loops_and_closes_pool(monkeypatch):
    cancelled = {"dispatcher": False, "health": False}

    async def run_until_cancelled(name, _stop):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled[name] = True
            raise

    runtime = SimpleNamespace(
        dispatcher=SimpleNamespace(
            run_forever=lambda stop: run_until_cancelled("dispatcher", stop)
        ),
        health=SimpleNamespace(
            run_once=AsyncMock(return_value=True),
            run_forever=lambda stop: run_until_cancelled("health", stop),
        ),
        pool_manager=SimpleNamespace(close_all=AsyncMock()),
    )
    build = AsyncMock(return_value=runtime)
    monkeypatch.setattr("server.app._build_provisioning_runtime", build)
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))
    config = TenantProvisioningConfig()

    async with provisioning_runtime_lifespan(app, AsyncMock(), config):
        await asyncio.sleep(0)
        runtime.health.run_once.assert_awaited_once()
        assert len(app.state.background_tasks) == 2

    assert cancelled == {"dispatcher": True, "health": True}
    runtime.pool_manager.close_all.assert_awaited_once()
    assert all(task.done() for task in app.state.background_tasks)


async def test_provisioning_lifespan_starts_without_unique_instance_gate(monkeypatch):
    stop = asyncio.Event()
    stop.set()
    runtime = SimpleNamespace(
        dispatcher=SimpleNamespace(run_forever=AsyncMock()),
        health=SimpleNamespace(
            run_once=AsyncMock(return_value=0),
            run_forever=AsyncMock(),
        ),
        pool_manager=SimpleNamespace(close_all=AsyncMock()),
    )
    build = AsyncMock(return_value=runtime)
    monkeypatch.setattr("server.app._build_provisioning_runtime", build)
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))

    async with provisioning_runtime_lifespan(
        app, AsyncMock(), TenantProvisioningConfig(enabled=False)
    ):
        pass

    build.assert_awaited_once()
    runtime.health.run_once.assert_awaited_once()
    runtime.pool_manager.close_all.assert_awaited_once()
