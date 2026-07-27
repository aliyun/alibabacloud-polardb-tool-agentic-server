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
from server.core.crypto import decrypt, encrypt
from server.core.binding_manager import resolve_user_instance_access
from server.core.tenant_provisioner import generate_tenant_name, ensure_tenant
from server.db import engine as engine_mod
from server.db.engine import reset_engine
from server.mcp.tools import resolve_target_instance
from server.mcp.transport import reset_mcp
from server.models import (
    AllocationMode,
    AuthProvider,
    Base,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    Permission,
    TenantProvisioningStep,
    User,
    UserInstanceBinding,
    UserRole,
    UserStatus,
)
from server.models.binding import DepartmentInstanceBinding, UserDepartment
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
    async def test_instance_topology_multitenant(self, session):
        inst = Instance(
            cluster_id="pc-mt-001", name="mt-cluster",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        await session.refresh(inst)
        assert inst.topology == InstanceTopology.MULTITENANT
        assert inst.owner_user_id is None

    async def test_provisioning_admin_credential(self, session):
        inst = Instance(
            cluster_id="pc-mt-002", name="mt-cluster",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        credential = InstanceCredential(
            instance_id=inst.id,
            name="root",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)
        assert credential.created_by_user_id is None
        assert credential.purpose == CredentialPurpose.PROVISIONING_ADMIN

    async def test_tenant_binding_with_step(self, session):
        inst = Instance(
            cluster_id="pc-mt-003", name="mt-cluster",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="mt-user-1", display_name="Test", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        credential = InstanceCredential(
            instance_id=inst.id,
            name="agentic@ta1b2c3d4",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
            database_name="agentic@ta1b2c3d4",
            created_by_user_id=user.id,
        )
        session.add(credential)
        await session.flush()
        binding = UserInstanceBinding(
            instance_id=inst.id,
            user_id=user.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
            origin=BindingOrigin.SYSTEM,
            tenant_name="ta1b2c3d4",
            provisioning_step=TenantProvisioningStep.PENDING,
        )
        session.add(binding)
        await session.commit()
        await session.refresh(binding)
        assert binding.tenant_name == "ta1b2c3d4"
        assert binding.provisioning_step == TenantProvisioningStep.PENDING

    async def test_tenant_binding_step_null_when_done(self, session):
        inst = Instance(
            cluster_id="pc-mt-004", name="mt-cluster",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="mt-user-2", display_name="Test2", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        credential = InstanceCredential(
            instance_id=inst.id,
            name="agentic@tb5c6d7e8",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
            created_by_user_id=user.id,
        )
        session.add(credential)
        await session.flush()
        binding = UserInstanceBinding(
            instance_id=inst.id,
            user_id=user.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
            origin=BindingOrigin.SYSTEM,
            tenant_name="tb5c6d7e8",
            provisioning_step=None,
        )
        session.add(binding)
        await session.commit()
        await session.refresh(binding)
        assert binding.provisioning_step is None


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
            cluster_id="pc-tn-001", name="mt",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        name = await generate_tenant_name(session, inst.id, "550e8400-e29b-41d4-a716-446655440000")
        assert name == "t550e8400"
        assert len(name) <= 10

    async def test_collision_adds_suffix(self, session):
        inst = Instance(
            cluster_id="pc-tn-002", name="mt",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        other_user = User(external_id="other-user", display_name="Other", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, other_user])
        await session.commit()
        existing = UserInstanceBinding(
            instance_id=inst.id,
            user_id=other_user.id,
            permission=Permission.READWRITE,
            origin=BindingOrigin.SYSTEM,
            tenant_name="t550e8400",
        )
        session.add(existing)
        await session.commit()
        name = await generate_tenant_name(session, inst.id, "550e8400-xxxx-yyyy-zzzz-000000000000")
        assert name == "t550e8402"
        assert len(name) <= 10


class TestEnsureTenant:
    @pytest.mark.parametrize(
        "mutation",
        [
            "wrong_instance",
            "resource_owned",
            "provisioning_admin",
            "revoked",
            "admin",
            "missing_ciphertext",
        ],
    )
    async def test_resume_rejects_invalid_credential_before_decrypt(
        self, session, mutation
    ):
        inst = Instance(
            cluster_id=f"pc-et-invalid-{mutation}",
            name="mt-invalid",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        other = Instance(
            cluster_id=f"pc-et-other-{mutation}",
            name="mt-other",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(
            external_id=f"et-invalid-{mutation}",
            display_name="Invalid Credential User",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add_all([inst, other, user])
        await session.commit()
        stored = InstanceCredential(
            instance_id=inst.id,
            name=f"direct-{mutation}",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("tenant-user"),
            password_ciphertext=encrypt("tenant-password"),
            created_by_user_id=user.id,
        )
        session.add(stored)
        await session.flush()
        session.add(
            UserInstanceBinding(
                instance_id=inst.id,
                user_id=user.id,
                credential_id=stored.id,
                permission=Permission.READWRITE,
                origin=BindingOrigin.SYSTEM,
                tenant_name="tinvalid",
                provisioning_step=TenantProvisioningStep.TENANT,
            )
        )
        await session.commit()

        invalid = InstanceCredential(
            id=stored.id,
            instance_id=inst.id,
            name=stored.name,
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            status=CredentialStatus.ACTIVE,
            username_ciphertext=stored.username_ciphertext,
            password_ciphertext=stored.password_ciphertext,
            created_by_user_id=user.id,
        )
        if mutation == "wrong_instance":
            invalid.instance_id = other.id
        elif mutation == "resource_owned":
            invalid.instance_id = None
            invalid.resource_id = "resource-id"
        elif mutation == "provisioning_admin":
            invalid.purpose = CredentialPurpose.PROVISIONING_ADMIN
        elif mutation == "revoked":
            invalid.status = CredentialStatus.REVOKED
        elif mutation == "admin":
            invalid.capability = CredentialCapability.ADMIN
        elif mutation == "missing_ciphertext":
            invalid.password_ciphertext = None

        original_get = session.get

        async def get_credential(model, object_id):
            if model is InstanceCredential and object_id == stored.id:
                return invalid
            return await original_get(model, object_id)

        with (
            patch.object(session, "get", side_effect=get_credential),
            patch("server.core.tenant_provisioner.decrypt") as decrypt_mock,
        ):
            with pytest.raises(
                RuntimeError, match="tenant credential is unavailable"
            ):
                await ensure_tenant(user, inst, session)

        decrypt_mock.assert_not_called()

    async def test_department_materialization_remains_system_scoped(
        self, session
    ):
        department = Department(name="Tenant Materialization Department")
        inst = Instance(
            cluster_id="pc-et-department",
            name="mt-department",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(
            external_id="et-department-user",
            display_name="Department Tenant User",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add_all([department, inst, user])
        await session.commit()
        department_binding = DepartmentInstanceBinding(
            department_id=department.id,
            instance_id=inst.id,
        )
        session.add_all(
            [
                department_binding,
                UserDepartment(
                    user_id=user.id,
                    department_id=department.id,
                ),
                InstanceCredential(
                    instance_id=inst.id,
                    name="root",
                    purpose=CredentialPurpose.PROVISIONING_ADMIN,
                    capability=CredentialCapability.ADMIN,
                    username_ciphertext=encrypt("root"),
                    password_ciphertext=encrypt("rootpwd"),
                ),
            ]
        )
        await session.commit()

        mock_conn, _mock_cursor = _make_mock_conn()
        with patch(
            "server.core.tenant_provisioner._connect_as_super",
            return_value=mock_conn,
        ):
            binding = await ensure_tenant(user, inst, session)

        assert binding.origin == BindingOrigin.SYSTEM
        assert (
            await resolve_user_instance_access(session, inst.id, user.id)
            is not None
        )

        await session.delete(department_binding)
        await session.commit()

        assert (
            await resolve_user_instance_access(session, inst.id, user.id)
            is None
        )

    async def test_department_readonly_permission_survives_repeated_calls(
        self, session
    ):
        inst = Instance(
            cluster_id="pc-et-readonly",
            name="mt-readonly",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(
            external_id="et-user-readonly",
            display_name="Readonly Tenant User",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add_all([inst, user])
        await session.commit()
        session.add(
            InstanceCredential(
                instance_id=inst.id,
                name="root",
                purpose=CredentialPurpose.PROVISIONING_ADMIN,
                capability=CredentialCapability.ADMIN,
                username_ciphertext=encrypt("root"),
                password_ciphertext=encrypt("rootpwd"),
            )
        )
        await session.commit()

        mock_conn, _mock_cursor = _make_mock_conn()
        with patch(
            "server.core.tenant_provisioner._connect_as_super",
            return_value=mock_conn,
        ):
            first = await ensure_tenant(
                user,
                inst,
                session,
                permission=Permission.READONLY,
            )
            second = await ensure_tenant(
                user,
                inst,
                session,
                permission=Permission.READONLY,
            )

        credential = await session.get(
            InstanceCredential, first.credential_id
        )
        assert first.id == second.id
        assert first.permission == Permission.READONLY
        assert credential is not None
        assert credential.capability == CredentialCapability.READONLY

    async def test_creates_tenant_account(self, session):
        inst = Instance(
            cluster_id="pc-et-001", name="mt",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="et-user-1", display_name="TenantUser", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        super_acct = InstanceCredential(
            instance_id=inst.id,
            name="root",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("rootpwd"),
        )
        session.add(super_acct)
        await session.commit()

        mock_conn, mock_cursor = _make_mock_conn()

        with patch("server.core.tenant_provisioner._connect_as_super", return_value=mock_conn):
            result = await ensure_tenant(user, inst, session)

        assert result.user_id == user.id
        assert result.tenant_name is not None
        assert result.provisioning_step is None
        credential = await session.get(InstanceCredential, result.credential_id)
        assert credential is not None
        assert credential.username_ciphertext is not None
        assert decrypt(credential.username_ciphertext).startswith("agentic@t")
        assert mock_cursor.execute.call_count == 5

    async def test_idempotent_second_call(self, session):
        inst = Instance(
            cluster_id="pc-et-002", name="mt",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="et-user-2", display_name="TenantUser2", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        super_acct = InstanceCredential(
            instance_id=inst.id,
            name="root",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("rootpwd"),
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
            cluster_id="pc-et-003", name="mt",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        user = User(external_id="et-user-3", display_name="TenantUser3", auth_provider=AuthProvider.BUILTIN)
        session.add_all([inst, user])
        await session.commit()
        super_acct = InstanceCredential(
            instance_id=inst.id,
            name="root",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("rootpwd"),
        )
        partial_credential = InstanceCredential(
            instance_id=inst.id,
            name="agentic@t" + user.id.replace("-", "")[:8],
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt(
                "agentic@t" + user.id.replace("-", "")[:8]
            ),
            password_ciphertext=encrypt("userpwd"),
            created_by_user_id=user.id,
        )
        session.add_all([super_acct, partial_credential])
        await session.flush()
        partial = UserInstanceBinding(
            instance_id=inst.id,
            user_id=user.id,
            credential_id=partial_credential.id,
            permission=Permission.READWRITE,
            origin=BindingOrigin.SYSTEM,
            tenant_name="t" + user.id.replace("-", "")[:8],
            provisioning_step=TenantProvisioningStep.TENANT,
        )
        session.add(partial)
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
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="127.0.0.1", port=3306,
        )
        session.add_all([dept, inst])
        await session.commit()
        super_acct = InstanceCredential(
            instance_id=inst.id,
            name="root",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("rootpwd"),
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
        assert instance.topology == InstanceTopology.MULTITENANT

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


@pytest.fixture(autouse=True)
def _stub_registered_connection(monkeypatch):
    async def healthy_connection(**_kwargs):
        return None

    monkeypatch.setattr(
        "server.core.instance_connection.test_mysql_connection",
        healthy_connection,
    )


async def _register_mt_instance(
    client: AsyncClient,
    cookies,
    *,
    cluster_id: str,
    name: str,
) -> str:
    response = await client.post(
        "/api/instances",
        json={
            "cluster_id": cluster_id,
            "name": name,
            "engine": "polardb_mysql",
            "topology": "multitenant",
            "host": "db.example.invalid",
            "port": 3306,
            "username": "provisioner",
            "password": "proxy_password",
        },
        cookies=cookies,
    )
    assert response.status_code == 201
    return response.json()["id"]


class TestMultitenantAdminAPI:
    @pytest.fixture
    async def client(self):
        reset_config()
        reset_engine()
        reset_mcp()
        os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

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

        instance_id = await _register_mt_instance(
            client,
            cookies,
            cluster_id="pc-api-001",
            name="api-mt-cluster",
        )
        resp = await client.post(
            f"/api/departments/{dept_id}/multitenant-instance",
            json={"instance_id": instance_id},
            cookies=cookies,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["instance"]["type"] == "multitenant"
        assert data["instance"]["status"] == "active"
        assert data["binding"]["department_id"] == dept_id

    async def test_unbind_multitenant_instance(self, client):
        cookies = await _login_admin(client)
        resp = await client.post("/api/departments", json={"name": "mt-unbind-dept"}, cookies=cookies)
        dept_id = resp.json()["id"]
        instance_id = await _register_mt_instance(
            client,
            cookies,
            cluster_id="pc-api-002",
            name="unbind-cluster",
        )
        resp = await client.post(
            f"/api/departments/{dept_id}/multitenant-instance",
            json={"instance_id": instance_id},
            cookies=cookies,
        )
        assert resp.status_code == 201

        resp = await client.delete(f"/api/departments/{dept_id}/multitenant-instance/{instance_id}", cookies=cookies)
        assert resp.status_code == 204


class TestTenantManagementAPI:
    @pytest.fixture
    async def client_with_mt(self):
        reset_config()
        reset_engine()
        reset_mcp()
        os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

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
            instance_id = await _register_mt_instance(
                c,
                cookies,
                cluster_id="pc-tm-001",
                name="tm-cluster",
            )
            inst_resp = await c.post(
                f"/api/departments/{dept_id}/multitenant-instance",
                json={"instance_id": instance_id},
                cookies=cookies,
            )
            assert inst_resp.status_code == 201
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

        async with engine_mod._session_factory() as session:
            binding = (
                await session.execute(
                    UserInstanceBinding.__table__.select().where(
                        UserInstanceBinding.instance_id == instance_id,
                        UserInstanceBinding.user_id == admin_id,
                    )
                )
            ).one()
            assert binding.origin == BindingOrigin.ADMIN
            assert (
                await resolve_user_instance_access(
                    session, instance_id, admin_id
                )
                is not None
            )

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
