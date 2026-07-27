"""Integration tests for application-level read-only enforcement."""
import base64
import json
import os
from types import SimpleNamespace
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
from server.core.sql_executor import reset_rate_limiters
from server.core.sql_gateway import SQLGateway
from server.db import engine as engine_mod
from server.mcp.tools import reset_gateway, set_gateway
from server.mcp.transport import reset_mcp
from server.models import (
    AuthProvider,
    Base,
    BindingCapability,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceStatus,
    InstanceCredential,
    InstanceTopology,
    AllocationMode,
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
    return e


@pytest.fixture
async def setup_readwrite(encryption_key):
    """Set up a READWRITE user with instance binding."""
    engine_mod._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_mod._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._session_factory = async_sessionmaker(
        engine_mod._engine, expire_on_commit=False
    )

    async with engine_mod._session_factory() as session:
        user = User(
            external_id="rwuser",
            display_name="ReadWrite User",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.MEMBER,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        instance = Instance(
            cluster_id="pc-test-rw",
            name="RW Instance",
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
            name="pas_rw_user", purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("pas_rw_user", key=encryption_key),
            password_ciphertext=encrypted_pw, created_by_user_id=user.id,
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)

        binding = UserInstanceBinding(
            user_id=user.id,
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

        return {"user": user, "instance": instance}


@pytest.fixture
async def setup_readonly(encryption_key):
    """Set up a READONLY user with instance binding."""
    engine_mod._engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine_mod._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._session_factory = async_sessionmaker(
        engine_mod._engine, expire_on_commit=False
    )

    async with engine_mod._session_factory() as session:
        user = User(
            external_id="rouser",
            display_name="ReadOnly User",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.MEMBER,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        instance = Instance(
            cluster_id="pc-test-ro",
            name="RO Instance",
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
            name="pas_ro_user", purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("pas_ro_user", key=encryption_key),
            password_ciphertext=encrypted_pw, created_by_user_id=user.id,
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)

        binding = UserInstanceBinding(
            user_id=user.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READONLY,
        )
        binding.capabilities = [
            UserInstanceBindingCapability(
                capability=BindingCapability.SQL_READ
            )
        ]
        session.add(binding)
        await session.commit()

        return {"user": user, "instance": instance}


@pytest.fixture
async def rw_client(setup_readwrite):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def ro_client(setup_readonly):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def rw_headers(setup_readwrite):
    token = create_access_token(
        {"sub": setup_readwrite["user"].id, "role": "member"}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def ro_headers(setup_readonly):
    token = create_access_token(
        {"sub": setup_readonly["user"].id, "role": "member"}
    )
    return {"Authorization": f"Bearer {token}"}


class TestReadonlyEnforcement:
    """READONLY users should have write SQL rejected at the application level."""

    async def test_readonly_user_can_select(
        self, ro_client, ro_headers, setup_readonly
    ):
        """READONLY user can run SELECT."""
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "SELECT * FROM users",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result["permission"] == "readonly"
        # Goes through mock gateway — not rejected
        assert result["rows"] == [["mock"]]

    async def test_readonly_user_can_show(
        self, ro_client, ro_headers, setup_readonly
    ):
        """READONLY user can run SHOW."""
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "SHOW TABLES",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result["permission"] == "readonly"

    async def test_readonly_user_cannot_insert(
        self, ro_client, ro_headers, setup_readonly
    ):
        """READONLY user is rejected for INSERT."""
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "INSERT INTO users (name) VALUES ('test')",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result.get("error") == "READ_ONLY_ACCESS"

    async def test_readonly_user_cannot_update(
        self, ro_client, ro_headers, setup_readonly
    ):
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "UPDATE users SET name = 'x' WHERE id = 1",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result.get("error") == "READ_ONLY_ACCESS"

    async def test_readonly_user_cannot_delete(
        self, ro_client, ro_headers, setup_readonly
    ):
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "DELETE FROM users WHERE id = 1",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result.get("error") == "READ_ONLY_ACCESS"

    async def test_readonly_user_cannot_create_table(
        self, ro_client, ro_headers, setup_readonly
    ):
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "CREATE TABLE test (id INT)",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result.get("error") == "READ_ONLY_ACCESS"

    async def test_readonly_user_branch_tool_passes_readonly_to_database(
        self, ro_client, ro_headers, setup_readonly
    ):
        """Branch tools preserve the caller's read-only restriction."""
        gateway = SimpleNamespace(execute=AsyncMock(return_value={
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
        }))

        with patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway):
            resp = await ro_client.post(
                "/mcp/rest/create_branch",
                json={
                    "branch_name": "br_readonly_delegate",
                    "instance_id": setup_readonly["instance"].id,
                },
                headers=ro_headers,
            )

        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result == {
            "branch_name": "br_readonly_delegate",
            "status": "created",
        }
        assert gateway.execute.await_args.kwargs["sql"] == "CREATE BRANCH br_readonly_delegate"
        assert gateway.execute.await_args.kwargs["read_only"] is True
        assert gateway.execute.await_args.kwargs["branch"] == ""


class TestReadwriteAllowed:
    """READWRITE users can run all SQL types."""

    async def test_readwrite_user_can_select(
        self, rw_client, rw_headers, setup_readwrite
    ):
        resp = await rw_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "SELECT * FROM users",
                "instance_id": setup_readwrite["instance"].id,
            },
            headers=rw_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result["permission"] == "readwrite"

    async def test_readwrite_user_can_insert(
        self, rw_client, rw_headers, setup_readwrite
    ):
        resp = await rw_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "INSERT INTO users (name) VALUES ('test')",
                "instance_id": setup_readwrite["instance"].id,
            },
            headers=rw_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result["permission"] == "readwrite"
        # Goes through mock gateway — not rejected
        assert result["rows"] == [["mock"]]


class TestPermissionInResponse:
    """Verify 'permission' field appears in all run_sql responses."""

    async def test_permission_readwrite(
        self, rw_client, rw_headers, setup_readwrite
    ):
        resp = await rw_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "SELECT 1",
                "instance_id": setup_readwrite["instance"].id,
            },
            headers=rw_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result["permission"] == "readwrite"

    async def test_permission_readonly(
        self, ro_client, ro_headers, setup_readonly
    ):
        resp = await ro_client.post(
            "/mcp/rest/run_sql",
            json={
                "sql": "SELECT 1",
                "instance_id": setup_readonly["instance"].id,
            },
            headers=ro_headers,
        )
        assert resp.status_code == 200
        result = json.loads(resp.json()["content"][0]["text"])
        assert result["permission"] == "readonly"
