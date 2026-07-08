"""End-to-end regression test: full flow from login to SQL execution."""
import base64
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client
from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.connection_cache import ConnectionCache
from server.core.sql_executor import reset_rate_limiters
from server.core.sql_gateway import SQLGateway
from server.db import engine as engine_mod
from server.mcp.tools import set_gateway, reset_gateway
from server.mcp.transport import reset_mcp
from server.models import (
    Base, User, AuthProvider, UserRole,
)


_MOCK_SQL_RESULT = {
    "columns": ["result"],
    "rows": [["mock"]],
    "row_count": 1,
    "truncated": False,
}


@pytest.fixture(autouse=True)
def clean():
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    reset_mcp()
    set_polardb_client(MockPolarDBClient())
    reset_rate_limiters()

    mock_cache = AsyncMock(spec=ConnectionCache)
    gateway = SQLGateway(mock_cache)

    async def mock_execute(**kwargs):
        return _MOCK_SQL_RESULT.copy()

    async def mock_execute_transaction(**kwargs):
        return [_MOCK_SQL_RESULT.copy() for _ in kwargs.get("sql_statements", [])]

    with patch.object(gateway, "execute", side_effect=mock_execute), \
         patch.object(gateway, "execute_transaction", side_effect=mock_execute_transaction):
        set_gateway(gateway)
        yield
    reset_gateway()
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()
    reset_polardb_client()
    reset_rate_limiters()


@pytest.fixture
def encryption_key():
    key = os.urandom(32)
    key_b64 = base64.b64encode(key).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key_b64
    yield key
    if "PAS_ENCRYPTION_KEY" in os.environ:
        del os.environ["PAS_ENCRYPTION_KEY"]


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
async def setup(test_engine, encryption_key):
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("admin123"),
            role=UserRole.ADMIN,
        )
        member = User(
            external_id="employee1",
            display_name="Employee One",
            email="emp1@test.com",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("emp123"),
            role=UserRole.MEMBER,
        )
        session.add_all([admin, member])
        await session.commit()
        await session.refresh(admin)
        await session.refresh(member)
        return {"admin": admin, "member": member}


@pytest.fixture
async def client(setup):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestE2EFlow:
    """Full flow: login -> create dept -> register instance -> bind -> run_sql -> audit."""

    async def test_complete_admin_flow(self, client, setup, encryption_key):
        admin = setup["admin"]

        # Step 1: Admin login
        resp = await client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        admin_token = resp.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # Step 2: Create department
        resp = await client.post("/api/departments", json={"name": "Engineering", "description": "Eng team"}, headers=admin_headers)
        assert resp.status_code == 201
        dept_id = resp.json()["id"]

        # Step 3: Register instance
        resp = await client.post("/api/instances", json={
            "cluster_id": "pc-e2e-001",
            "name": "E2E Test DB",
            "type": "shared",
            "region": "cn-hangzhou",
        }, headers=admin_headers)
        assert resp.status_code == 201
        instance_id = resp.json()["id"]

        # Step 4: Bind user to instance
        resp = await client.post(f"/api/instances/{instance_id}/bind-user", json={
            "user_id": admin.id, "permission": "readwrite"
        }, headers=admin_headers)
        assert resp.status_code == 201

        # Step 5: Bind department to instance
        resp = await client.post(f"/api/instances/{instance_id}/bind-department", json={
            "department_id": dept_id, "tenant_name": "eng_tenant"
        }, headers=admin_headers)
        assert resp.status_code == 201

        # Step 6: List instances via MCP
        resp = await client.get("/mcp/rest/list_instances", headers=admin_headers)
        assert resp.status_code == 200
        instances = resp.json()["instances"]
        assert len(instances) >= 1
        assert any(i["cluster_id"] == "pc-e2e-001" for i in instances)

        # Step 7: Run SQL
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": instance_id,
        }, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert data.get("isError") is not True

        # Step 8: Verify audit log recorded
        resp = await client.get("/api/audit-logs", headers=admin_headers)
        assert resp.status_code == 200
        logs = resp.json()
        assert logs["total"] >= 1

        # Step 9: Run blocked SQL (DROP DATABASE is always blocked)
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP DATABASE mydb",
            "instance_id": instance_id,
        }, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "BLOCKED_SQL"

    async def test_disabled_user_blocked(self, client, setup, encryption_key):
        """Disabling a user blocks subsequent SQL execution immediately."""
        member = setup["member"]

        # Admin login
        resp = await client.post("/auth/login", json={"username": "admin", "password": "admin123"})
        admin_token = resp.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        # Member login
        resp = await client.post("/auth/login", json={"username": "employee1", "password": "emp123"})
        member_token = resp.json()["access_token"]
        member_headers = {"Authorization": f"Bearer {member_token}"}

        # Member can access /auth/me
        resp = await client.get("/auth/me", headers=member_headers)
        assert resp.status_code == 200

        # Admin disables member
        resp = await client.put(f"/api/users/{member.id}/disable", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

        # Member's next request is blocked
        resp = await client.get("/auth/me", headers=member_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "USER_DISABLED"

    async def test_unauthenticated_mcp_rejected(self, client):
        """MCP tools require authentication."""
        resp = await client.post("/mcp/rest/run_sql", json={"sql": "SELECT 1"})
        assert resp.status_code in (401, 403)

        resp = await client.get("/mcp/rest/list_instances")
        assert resp.status_code in (401, 403)

    async def test_member_cannot_access_admin_api(self, client, setup):
        """Members cannot access admin-only endpoints."""
        resp = await client.post("/auth/login", json={"username": "employee1", "password": "emp123"})
        member_token = resp.json()["access_token"]
        member_headers = {"Authorization": f"Bearer {member_token}"}

        # Try to list users (admin only)
        resp = await client.get("/api/users", headers=member_headers)
        assert resp.status_code == 403

        # Try to create department (admin only)
        resp = await client.post("/api/departments", json={"name": "Hack"}, headers=member_headers)
        assert resp.status_code == 403
