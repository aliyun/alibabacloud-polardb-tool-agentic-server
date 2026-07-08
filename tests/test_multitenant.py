import base64
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.builtin import hash_password
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.crypto import encrypt
from server.core.tenant_provisioner import generate_tenant_name, ensure_tenant
from server.db import engine as engine_mod
from server.db.engine import reset_engine
from server.mcp.tools import resolve_target_instance
from server.mcp.transport import reset_mcp
from server.models import (
    Base, Instance, InstanceType, InstanceStatus,
    DBAccount, AccountType, User, AuthProvider,
    UserRole, UserStatus,
)
from server.models.binding import DepartmentInstanceBinding, UserDepartment
from server.models.db_account import TenantProvisioningStep
from server.models.department import Department
from server.models.user import ProvisioningMode


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    init_test_jwt_keys()
    key = base64.b64encode(os.urandom(32)).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key
    yield
    reset_config()
    os.environ.pop("PAS_ENCRYPTION_KEY", None)


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


class TestMultitenantModels:
    async def test_instance_type_multitenant(self, session):
        inst = Instance(
            cluster_id="pc-mt-001", name="mt-cluster",
            type=InstanceType.MULTITENANT, status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        await session.refresh(inst)
        assert inst.type == InstanceType.MULTITENANT
        assert inst.owner_user_id is None

    async def test_super_db_account_null_user(self, session):
        inst = Instance(
            cluster_id="pc-mt-002", name="mt-cluster",
            type=InstanceType.MULTITENANT, status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        account = DBAccount(
            instance_id=inst.id, user_id=None,
            account_name="root",
            account_password_enc="encrypted",
            account_type=AccountType.SUPER,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        assert account.user_id is None
        assert account.account_type == AccountType.SUPER

    async def test_tenant_db_account_with_step(self, session):
        inst = Instance(
            cluster_id="pc-mt-003", name="mt-cluster",
            type=InstanceType.MULTITENANT, status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="mt-user-1", display_name="Test", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        account = DBAccount(
            instance_id=inst.id, user_id=user.id,
            account_name="agentic@ta1b2c3d4",
            account_password_enc="encrypted",
            account_type=AccountType.NORMAL,
            tenant_name="ta1b2c3d4",
            provisioning_step=TenantProvisioningStep.PENDING,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        assert account.tenant_name == "ta1b2c3d4"
        assert account.provisioning_step == TenantProvisioningStep.PENDING

    async def test_provisioning_step_null_when_done(self, session):
        inst = Instance(
            cluster_id="pc-mt-004", name="mt-cluster",
            type=InstanceType.MULTITENANT, status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="mt-user-2", display_name="Test2", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        account = DBAccount(
            instance_id=inst.id, user_id=user.id,
            account_name="agentic@tb5c6d7e8",
            account_password_enc="encrypted",
            account_type=AccountType.NORMAL,
            tenant_name="tb5c6d7e8",
            provisioning_step=None,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        assert account.provisioning_step is None


def _make_mock_conn():
    mock_cursor = AsyncMock()
    mock_conn = MagicMock()

    @asynccontextmanager
    async def cursor_ctx():
        yield mock_cursor

    mock_conn.cursor = cursor_ctx
    mock_conn.close = MagicMock()
    return mock_conn, mock_cursor


class TestGenerateTenantName:
    async def test_basic_name(self, session):
        inst = Instance(
            cluster_id="pc-tn-001", name="mt", type=InstanceType.MULTITENANT,
            status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        name = await generate_tenant_name(session, inst.id, "550e8400-e29b-41d4-a716-446655440000")
        assert name == "t550e8400"
        assert len(name) <= 10

    async def test_collision_adds_suffix(self, session):
        inst = Instance(
            cluster_id="pc-tn-002", name="mt", type=InstanceType.MULTITENANT,
            status=InstanceStatus.ACTIVE,
        )
        other_user = User(external_id="other-user", display_name="Other", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, other_user])
        await session.commit()
        existing = DBAccount(
            instance_id=inst.id, user_id=other_user.id,
            account_name="agentic@t550e8400",
            account_password_enc="enc", account_type=AccountType.NORMAL,
            tenant_name="t550e8400",
        )
        session.add(existing)
        await session.commit()
        name = await generate_tenant_name(session, inst.id, "550e8400-xxxx-yyyy-zzzz-000000000000")
        assert name == "t550e8402"
        assert len(name) <= 10


class TestEnsureTenant:
    async def test_creates_tenant_account(self, session):
        inst = Instance(
            cluster_id="pc-et-001", name="mt", type=InstanceType.MULTITENANT,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="et-user-1", display_name="TenantUser", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        super_acct = DBAccount(
            instance_id=inst.id, user_id=None,
            account_name="root",
            account_password_enc=encrypt("rootpwd"),
            account_type=AccountType.SUPER,
        )
        session.add(super_acct)
        await session.commit()

        mock_conn, mock_cursor = _make_mock_conn()

        with patch("server.core.tenant_provisioner._connect_as_super", return_value=mock_conn):
            result = await ensure_tenant(user, inst, session)

        assert result.user_id == user.id
        assert result.tenant_name is not None
        assert result.provisioning_step is None
        assert result.account_name.startswith("agentic@t")
        assert mock_cursor.execute.call_count == 5

    async def test_idempotent_second_call(self, session):
        inst = Instance(
            cluster_id="pc-et-002", name="mt", type=InstanceType.MULTITENANT,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="et-user-2", display_name="TenantUser2", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        super_acct = DBAccount(
            instance_id=inst.id, user_id=None,
            account_name="root",
            account_password_enc=encrypt("rootpwd"),
            account_type=AccountType.SUPER,
        )
        session.add(super_acct)
        await session.commit()

        mock_conn, mock_cursor = _make_mock_conn()

        with patch("server.core.tenant_provisioner._connect_as_super", return_value=mock_conn):
            first = await ensure_tenant(user, inst, session)
            mock_cursor.execute.reset_mock()
            second = await ensure_tenant(user, inst, session)

        assert first.id == second.id
        assert mock_cursor.execute.call_count == 0

    async def test_resumes_from_failed_step(self, session):
        inst = Instance(
            cluster_id="pc-et-003", name="mt", type=InstanceType.MULTITENANT,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="et-user-3", display_name="TenantUser3", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        super_acct = DBAccount(
            instance_id=inst.id, user_id=None,
            account_name="root",
            account_password_enc=encrypt("rootpwd"),
            account_type=AccountType.SUPER,
        )
        partial = DBAccount(
            instance_id=inst.id, user_id=user.id,
            account_name="agentic@t" + user.id.replace("-", "")[:8],
            account_password_enc=encrypt("userpwd"),
            account_type=AccountType.NORMAL,
            tenant_name="t" + user.id.replace("-", "")[:8],
            provisioning_step=TenantProvisioningStep.TENANT,
        )
        session.add_all([super_acct, partial])
        await session.commit()

        mock_conn, mock_cursor = _make_mock_conn()

        with patch("server.core.tenant_provisioner._connect_as_super", return_value=mock_conn):
            result = await ensure_tenant(user, inst, session)

        assert result.provisioning_step is None
        assert mock_cursor.execute.call_count == 3


class TestRunSqlMultitenantIntegration:
    async def _seed_multitenant(self, session):
        dept = Department(name="mt-dept")
        inst = Instance(
            cluster_id="pc-int-001", name="mt-cluster",
            type=InstanceType.MULTITENANT, status=InstanceStatus.ACTIVE,
            host="127.0.0.1", port=3306,
        )
        session.add_all([dept, inst])
        await session.commit()
        super_acct = DBAccount(
            instance_id=inst.id, user_id=None,
            account_name="root",
            account_password_enc=encrypt("rootpwd"),
            account_type=AccountType.SUPER,
        )
        binding = DepartmentInstanceBinding(
            department_id=dept.id, instance_id=inst.id,
        )
        user = User(
            external_id="int-user-1", display_name="IntUser",
            auth_provider=AuthProvider.BUILTIN,
            provisioning_mode=ProvisioningMode.MULTITENANT,
        )
        session.add_all([super_acct, binding, user])
        await session.commit()
        membership = UserDepartment(user_id=user.id, department_id=dept.id)
        session.add(membership)
        await session.commit()
        await session.refresh(user)
        return user, inst

    async def test_resolve_returns_multitenant_instance(self, session):
        user, inst = await self._seed_multitenant(session)
        result = await resolve_target_instance(user, session)
        assert isinstance(result, tuple)
        instance, accessible = result
        assert isinstance(instance, Instance)
        assert instance.type == InstanceType.MULTITENANT

    async def test_no_multitenant_instance_returns_error(self, session):
        user = User(
            external_id="no-mt-user", display_name="NoMT",
            auth_provider=AuthProvider.BUILTIN,
            provisioning_mode=ProvisioningMode.MULTITENANT,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        result = await resolve_target_instance(user, session)
        assert isinstance(result, dict)
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "NO_MULTITENANT_INSTANCE"


_ADMIN_PASSWORD = "TestPass123"


async def _login_admin(client: AsyncClient):
    resp = await client.post("/auth/login", json={"username": "admin", "password": _ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.cookies


class TestMultitenantAdminAPI:
    @pytest.fixture
    async def client(self):
        reset_config()
        reset_engine()
        reset_mcp()
        os.environ["PAS_SERVER_DEV_MODE"] = "true"
        os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
        os.environ["PAS_ADMIN_INITIAL_PASSWORD"] = _ADMIN_PASSWORD

        e = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with e.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        engine_mod._engine = e
        engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)

        async with engine_mod._session_factory() as s:
            admin = User(
                external_id="admin", display_name="Administrator",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password(_ADMIN_PASSWORD),
                role=UserRole.ADMIN, status=UserStatus.ACTIVE,
            )
            s.add(admin)
            await s.commit()

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
        await e.dispose()
        reset_config()
        reset_engine()
        reset_mcp()

    async def test_register_multitenant_instance(self, client):
        cookies = await _login_admin(client)
        resp = await client.post("/api/departments", json={"name": "mt-api-dept"}, cookies=cookies)
        assert resp.status_code == 201
        dept_id = resp.json()["id"]

        resp = await client.post(f"/api/departments/{dept_id}/multitenant-instance", json={
            "cluster_id": "pc-api-001",
            "name": "api-mt-cluster",
            "host": "pc-api.rds.aliyuncs.com",
            "port": 3306,
            "region": "cn-hangzhou",
            "admin_account": "root",
            "admin_password": "adminpwd123",
        }, cookies=cookies)
        assert resp.status_code == 201
        data = resp.json()
        assert data["instance"]["type"] == "multitenant"
        assert data["instance"]["status"] == "active"
        assert data["binding"]["department_id"] == dept_id

    async def test_unbind_multitenant_instance(self, client):
        cookies = await _login_admin(client)
        resp = await client.post("/api/departments", json={"name": "mt-unbind-dept"}, cookies=cookies)
        dept_id = resp.json()["id"]
        resp = await client.post(f"/api/departments/{dept_id}/multitenant-instance", json={
            "cluster_id": "pc-api-002", "name": "unbind-cluster",
            "host": "h", "port": 3306, "admin_account": "root", "admin_password": "pwd",
        }, cookies=cookies)
        instance_id = resp.json()["instance"]["id"]

        resp = await client.delete(f"/api/departments/{dept_id}/multitenant-instance/{instance_id}", cookies=cookies)
        assert resp.status_code == 204


class TestTenantManagementAPI:
    @pytest.fixture
    async def client_with_mt(self):
        reset_config()
        reset_engine()
        reset_mcp()
        os.environ["PAS_SERVER_DEV_MODE"] = "true"
        os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
        os.environ["PAS_ADMIN_INITIAL_PASSWORD"] = _ADMIN_PASSWORD

        e = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with e.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        engine_mod._engine = e
        engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)

        async with engine_mod._session_factory() as s:
            admin = User(
                external_id="admin", display_name="Administrator",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password(_ADMIN_PASSWORD),
                role=UserRole.ADMIN, status=UserStatus.ACTIVE,
            )
            s.add(admin)
            await s.commit()

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cookies = await _login_admin(c)
            dept_resp = await c.post("/api/departments", json={"name": "tenant-mgmt-dept"}, cookies=cookies)
            dept_id = dept_resp.json()["id"]
            inst_resp = await c.post(f"/api/departments/{dept_id}/multitenant-instance", json={
                "cluster_id": "pc-tm-001", "name": "tm-cluster",
                "host": "127.0.0.1", "port": 3306,
                "admin_account": "root", "admin_password": "pwd",
            }, cookies=cookies)
            instance_id = inst_resp.json()["instance"]["id"]
            yield c, cookies, dept_id, instance_id
        await e.dispose()
        reset_config()
        reset_engine()
        reset_mcp()

    async def test_list_tenants_empty(self, client_with_mt):
        c, cookies, _, instance_id = client_with_mt
        resp = await c.get(f"/api/instances/{instance_id}/tenants", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list_tenant(self, client_with_mt):
        c, cookies, _, instance_id = client_with_mt
        users_resp = await c.get("/api/users", cookies=cookies)
        admin_id = users_resp.json()["items"][0]["id"]

        mock_conn, mock_cursor = _make_mock_conn()
        with patch("server.core.tenant_provisioner._connect_as_super", return_value=mock_conn):
            resp = await c.post(f"/api/instances/{instance_id}/tenants",
                json={"user_id": admin_id}, cookies=cookies)
            assert resp.status_code == 201

        resp = await c.get(f"/api/instances/{instance_id}/tenants", cookies=cookies)
        assert resp.status_code == 200
        tenants = resp.json()
        assert len(tenants) == 1
        assert tenants[0]["user_id"] == admin_id
        assert tenants[0]["tenant_name"] is not None

    async def test_delete_tenant(self, client_with_mt):
        c, cookies, _, instance_id = client_with_mt
        users_resp = await c.get("/api/users", cookies=cookies)
        admin_id = users_resp.json()["items"][0]["id"]

        mock_conn, mock_cursor = _make_mock_conn()
        with patch("server.core.tenant_provisioner._connect_as_super", return_value=mock_conn):
            await c.post(f"/api/instances/{instance_id}/tenants",
                json={"user_id": admin_id}, cookies=cookies)

        resp = await c.delete(f"/api/instances/{instance_id}/tenants/{admin_id}", cookies=cookies)
        assert resp.status_code == 204

        resp = await c.get(f"/api/instances/{instance_id}/tenants", cookies=cookies)
        assert resp.json() == []
