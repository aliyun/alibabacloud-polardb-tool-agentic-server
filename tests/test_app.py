
import pytest
from httpx import ASGITransport, AsyncClient

from server.app import create_app
from server.config import reset_config
from server.mcp.transport import reset_mcp


@pytest.fixture(autouse=True)
def clean():
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


class TestRequestID:
    async def test_response_has_request_id(self, client):
        resp = await client.get("/livez")
        assert "x-request-id" in resp.headers

    async def test_custom_request_id_preserved(self, client):
        resp = await client.get("/livez", headers={"X-Request-ID": "test-123"})
        assert resp.headers["x-request-id"] == "test-123"
