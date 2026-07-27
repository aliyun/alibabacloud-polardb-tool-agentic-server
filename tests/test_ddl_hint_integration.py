"""Integration tests for DDL COMMENT hint in handle_run_sql response."""
import base64
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import (
    MockPolarDBClient,
    reset_polardb_client,
    set_polardb_client,
)
from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import create_access_token, reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.connection_cache import ConnectionCache
from server.core.crypto import encrypt
from server.core.ddl_hints import DDL_COMMENT_HINT
from server.core.sql_executor import reset_rate_limiters
from server.core.sql_gateway import SQLGateway
from server.db import engine as engine_mod
from server.mcp.tools import reset_gateway, set_gateway
from server.mcp.transport import reset_mcp
from server.models import (
    AuthProvider,
    Base,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceStatus,
    InstanceCredential,
    InstanceTopology,
    AllocationMode,
    BindingCapability,
    Permission,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
    UserRole,
)


_MOCK_SQL_RESULT = {
    "columns": ["result"],
    "rows": [["mock"]],
    "row_count": 1,
    "truncated": False,
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
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

    with patch.object(gateway, "execute", side_effect=mock_execute):
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
async def setup_data(test_engine, encryption_key):
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)

        instance = Instance(
            cluster_id="pc-test-001",
            name="Test Instance",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            host="127.0.0.1",
            port=3306,
            status=InstanceStatus.ACTIVE,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)

        encrypted_pw = encrypt("test_password", key=encryption_key)
        credential = InstanceCredential(
            instance_id=instance.id,
            name="pas_admin", purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("pas_admin", key=encryption_key),
            password_ciphertext=encrypted_pw, created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)

        binding = UserInstanceBinding(
            user_id=admin.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
        )
        binding.capabilities = [
            UserInstanceBindingCapability(
                capability=BindingCapability.SQL_READ
            ),
            UserInstanceBindingCapability(
                capability=BindingCapability.SQL_WRITE
            ),
        ]
        session.add(binding)
        await session.commit()

        return {"admin": admin, "instance": instance}


@pytest.fixture
async def client(setup_data):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers(setup_data):
    token = create_access_token({"sub": setup_data["admin"].id, "role": "admin"})
    return {"Authorization": f"Bearer {token}"}


class TestDDLHintIntegration:
    async def test_create_table_without_comment_has_hint(
        self, client, auth_headers, setup_data
    ):
        """CREATE TABLE without COMMENT should include hint in response."""
        resp = await client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(100))",
                "instance_id": setup_data["instance"].id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result.get("hint") == DDL_COMMENT_HINT

    async def test_create_table_with_comment_no_hint(
        self, client, auth_headers, setup_data
    ):
        """CREATE TABLE that already has COMMENT should NOT include hint."""
        resp = await client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": (
                    "CREATE TABLE users (id INT PRIMARY KEY, "
                    "name VARCHAR(100) COMMENT 'username') "
                    "COMMENT='user table'"
                ),
                "instance_id": setup_data["instance"].id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert "hint" not in result

    async def test_select_no_hint(self, client, auth_headers, setup_data):
        """SELECT statements should not get a DDL hint."""
        resp = await client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "SELECT 1",
                "instance_id": setup_data["instance"].id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert "hint" not in result

    async def test_alter_table_add_column_without_comment_has_hint(
        self, client, auth_headers, setup_data
    ):
        """ALTER TABLE ADD COLUMN without COMMENT (with confirm) should include hint."""
        resp = await client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "ALTER TABLE users ADD COLUMN age INT",
                "instance_id": setup_data["instance"].id,
                "confirm": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result.get("hint") == DDL_COMMENT_HINT
