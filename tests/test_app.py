
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

    async def test_enterprise_identity_source_help_renders_html(self, client):
        response = await client.get(
            "/help/enterprise-identity-sources?locale=zh-CN"
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<h1>企业身份源接入</h1>" in response.text
        assert "飞书接入" in response.text
        assert "SharePoint 接入" in response.text
        assert "provider=feishu" in response.text
        assert "provider=sharepoint" in response.text
        assert "export PAS_URL='https://pas.example.com'" not in response.text
        assert 'aria-label="阅读字号"' in response.text

        feishu = await client.get(
            "/help/enterprise-identity-sources?locale=zh-CN&provider=feishu"
        )
        assert feishu.status_code == 200
        assert "<h1>飞书接入</h1>" in feishu.text
        assert "外部访问基础 URL" in feishu.text
        assert "配置 SharePoint 身份源" not in feishu.text
        assert (
            '/help/enterprise-identity-sources-api?locale=zh-cn'
            in feishu.text
        )
        assert "&amp;#x27;" not in feishu.text
        assert "&amp;quot;" not in feishu.text

        sharepoint = await client.get(
            "/help/enterprise-identity-sources?locale=zh-CN&provider=sharepoint"
        )
        assert sharepoint.status_code == 200
        assert "<h1>SharePoint 接入</h1>" in sharepoint.text
        assert "配置 SharePoint 身份源" in sharepoint.text
        assert "配置飞书身份源" not in sharepoint.text

        reference = await client.get(
            "/help/enterprise-identity-sources-api?locale=zh-CN"
        )
        assert reference.status_code == 200
        assert "<h1>企业身份源管理员 API</h1>" in reference.text
        assert "POST /api/identity-sources" in reference.text

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
    cancelled = {"dispatcher": False, "health": False, "dedicated": False}

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
        dedicated=SimpleNamespace(
            run_forever=lambda stop: run_until_cancelled("dedicated", stop)
        ),
        pool_manager=SimpleNamespace(close_all=AsyncMock()),
    )
    build = AsyncMock(return_value=runtime)
    monkeypatch.setattr("server.app._build_provisioning_runtime", build)
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))
    config = TenantProvisioningConfig(dedicated_pool_enabled=True)

    async with provisioning_runtime_lifespan(app, AsyncMock(), config):
        await asyncio.sleep(0)
        runtime.health.run_once.assert_awaited_once()
        # Dispatcher, health worker, runtime supervisor, and its Dedicated
        # child task are all tracked for coordinated shutdown.
        assert len(app.state.background_tasks) == 4

    assert cancelled == {
        "dispatcher": True,
        "health": True,
        "dedicated": True,
    }
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
        dedicated=SimpleNamespace(run_forever=AsyncMock()),
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


class TestDiscoverStaticDir:
    def _make_dist(self, root, *parts):
        d = root
        for p in parts:
            d = d / p
        d.mkdir(parents=True)
        (d / "index.html").write_text("<html></html>")
        return d

    def test_env_override_wins(self, tmp_path, monkeypatch):
        from server.app import _discover_static_dir

        override = self._make_dist(tmp_path, "custom-static")
        monkeypatch.setenv("PAS_STATIC_DIR", str(override))
        assert _discover_static_dir() == override

    def test_cwd_static_found_when_module_root_has_none(
        self, tmp_path, monkeypatch
    ):
        from server.app import _discover_static_dir

        static = self._make_dist(tmp_path, "static")
        monkeypatch.delenv("PAS_STATIC_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        found = _discover_static_dir()
        # The repo checkout may provide web/dist; otherwise the working
        # directory fallback (the packaged-image layout) must be used.
        assert found is not None
        assert found.name in ("dist", "static")
        if found.name == "static":
            assert found == static

    def test_none_when_nothing_exists(self, tmp_path, monkeypatch):
        from server.app import _discover_static_dir
        from server import app as app_module

        monkeypatch.delenv("PAS_STATIC_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            app_module,
            "__file__",
            str(tmp_path / "pkg" / "server" / "app.py"),
        )
        assert _discover_static_dir() is None


async def test_spa_fallback_uses_current_index_after_frontend_swap(
    tmp_path, monkeypatch
):
    static_dir = tmp_path / "dist"
    (static_dir / "assets").mkdir(parents=True)
    index = static_dir / "index.html"
    index.write_text("<html>old entry</html>")
    monkeypatch.setattr(
        "server.app._discover_static_dir", lambda: static_dir
    )
    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as http:
        first = await http.get(
            "/instances?type=polarrag",
            headers={"accept": "text/html"},
        )
        index.write_text("<html>new entry</html>")
        second = await http.get(
            "/instances?type=polarrag",
            headers={"accept": "text/html"},
        )

    assert first.text == "<html>old entry</html>"
    assert second.text == "<html>new entry</html>"
