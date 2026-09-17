
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client
from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import create_access_token, reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.models import (
    AuditLog,
    AuditStatus,
    Base,
    User,
    AuthProvider,
    UserRefreshToken,
    UserRole,
)

_REGISTERED_CONNECTION = {
    "host": "db.example.invalid",
    "port": 3306,
    "username": "proxy_user",
    "password": "proxy_password",
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    set_polardb_client(MockPolarDBClient())
    async def healthy_connection(**_kwargs):
        return None
    monkeypatch.setattr(
        "server.core.instance_connection.test_mysql_connection",
        healthy_connection,
    )
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_polardb_client()


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
async def admin_user(test_engine):
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with engine_mod._session_factory() as session:
        user = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.ADMIN,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def client(admin_user):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def admin_headers(admin_user):
    token = create_access_token({"sub": admin_user.id, "role": "admin", "credential_epoch": admin_user.credential_epoch})
    return {"Authorization": f"Bearer {token}"}


class TestDepartmentAPI:
    async def test_create_and_list(self, client, admin_headers):
        # Create
        resp = await client.post("/api/departments", json={"name": "Engineering"}, headers=admin_headers)
        assert resp.status_code == 201
        dept_id = resp.json()["id"]

        # List
        resp = await client.get("/api/departments", headers=admin_headers)
        assert resp.status_code == 200
        assert any(d["id"] == dept_id for d in resp.json()["items"])

    async def test_update_department(self, client, admin_headers):
        resp = await client.post("/api/departments", json={"name": "Sales"}, headers=admin_headers)
        dept_id = resp.json()["id"]

        resp = await client.put(f"/api/departments/{dept_id}", json={"name": "Sales Updated"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Sales Updated"

    async def test_delete_empty_department(self, client, admin_headers):
        resp = await client.post("/api/departments", json={"name": "ToDelete"}, headers=admin_headers)
        dept_id = resp.json()["id"]
        resp = await client.delete(f"/api/departments/{dept_id}", headers=admin_headers)
        assert resp.status_code == 204

    async def test_department_response_includes_agentic_fields(self, client, admin_headers):
        resp = await client.post("/api/departments", json={"name": "AgenticDept"}, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert "agentic_db_cluster_id" in data
        assert data["agentic_db_cluster_id"] is None
        assert "agentic_db_cluster_description" in data
        assert data["agentic_db_cluster_description"] is None

    async def test_update_department_agentic_fields(self, client, admin_headers):
        resp = await client.post("/api/departments", json={"name": "AgenticDept2"}, headers=admin_headers)
        dept_id = resp.json()["id"]

        resp = await client.put(
            f"/api/departments/{dept_id}",
            json={"agentic_db_cluster_id": "pagc-manual-123", "agentic_db_cluster_description": "Manual Set"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["agentic_db_cluster_id"] == "pagc-manual-123"
        assert resp.json()["agentic_db_cluster_description"] == "Manual Set"

    async def test_list_departments_includes_agentic_fields(self, client, admin_headers):
        resp = await client.post("/api/departments", json={"name": "AgenticList"}, headers=admin_headers)
        assert resp.status_code == 201

        resp = await client.get("/api/departments", headers=admin_headers)
        assert resp.status_code == 200
        dept = next(
            d for d in resp.json()["items"] if d["name"] == "AgenticList"
        )
        assert "agentic_db_cluster_id" in dept
        assert "agentic_db_cluster_description" in dept


class TestInstanceAPI:
    async def test_register_and_list(self, client, admin_headers):
        resp = await client.post("/api/instances", json={
            "cluster_id": "pc-test-001", "name": "Test DB",
            "engine": "polardb_mysql", "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        }, headers=admin_headers)
        assert resp.status_code == 201
        inst_id = resp.json()["id"]

        resp = await client.get("/api/instances", headers=admin_headers)
        assert resp.status_code == 200
        assert any(i["id"] == inst_id for i in resp.json()["items"])

    async def test_get_instance_detail(self, client, admin_headers):
        resp = await client.post("/api/instances", json={
            "cluster_id": "pc-detail-001", "name": "Detail DB",
            "engine": "polardb_mysql", "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        }, headers=admin_headers)
        inst_id = resp.json()["id"]

        resp = await client.get(f"/api/instances/{inst_id}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["cluster_id"] == "pc-detail-001"

    async def test_remove_instance(self, client, admin_headers):
        resp = await client.post("/api/instances", json={
            "cluster_id": "pc-remove", "name": "Remove DB",
            "engine": "polardb_mysql", "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        }, headers=admin_headers)
        inst_id = resp.json()["id"]

        resp = await client.delete(f"/api/instances/{inst_id}", headers=admin_headers)
        assert resp.status_code == 204


class TestUserAPI:
    async def test_delete_user_with_audit_history_requires_disable(
        self, client, admin_headers
    ):
        async with engine_mod._session_factory() as session:
            await session.execute(text("PRAGMA foreign_keys=ON"))
            assert await session.scalar(text("PRAGMA foreign_keys")) == 1
            member = User(
                external_id="delete-with-history",
                display_name="Delete With History",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password("delete-password-123"),
                role=UserRole.MEMBER,
            )
            session.add(member)
            await session.flush()
            session.add_all(
                [
                    UserRefreshToken(
                        user_id=member.id,
                        token_hash="delete-user-refresh-token",
                        token_family="delete-user-family",
                        expires_at=datetime.now(UTC),
                    ),
                    AuditLog(
                        actor_user_id=member.id,
                        action="user.test",
                        status=AuditStatus.SUCCESS,
                    ),
                ]
            )
            await session.commit()
            member_id = member.id

        response = await client.delete(
            f"/api/users/{member_id}", headers=admin_headers
        )

        assert response.status_code == 409
        assert response.json()["detail"] == {
            "error": "USER_HAS_AUDIT_HISTORY",
            "message": "User has audit history. Disable the user instead.",
        }
        async with engine_mod._session_factory() as session:
            assert await session.get(User, member_id) is not None
            assert await session.scalar(
                select(UserRefreshToken).where(
                    UserRefreshToken.user_id == member_id
                )
            )
            assert await session.scalar(
                select(AuditLog).where(AuditLog.actor_user_id == member_id)
            )

    async def test_list_users(self, client, admin_headers):
        resp = await client.get("/api/users", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    async def test_get_user_detail(self, client, admin_headers, admin_user):
        resp = await client.get(f"/api/users/{admin_user.id}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["external_id"] == "admin"

    async def test_create_user_rejects_password_shorter_than_shared_policy(
        self, client, admin_headers
    ):
        resp = await client.post(
            "/api/users",
            json={"username": "short-password", "password": "short123"},
            headers=admin_headers,
        )

        assert resp.status_code == 400
        assert "12 characters" in resp.json()["detail"]

    async def test_reset_password_rejects_password_shorter_than_shared_policy(
        self, client, admin_headers, admin_user
    ):
        resp = await client.put(
            f"/api/users/{admin_user.id}/reset-password",
            json={"new_password": "short123"},
            headers=admin_headers,
        )

        assert resp.status_code == 400
        assert "12 characters" in resp.json()["detail"]

    async def test_reset_password_invalidates_only_target_user_sessions(
        self,
        client,
        admin_user,
    ):
        async with engine_mod._session_factory() as session:
            member = User(
                external_id="reset-target",
                display_name="Reset Target",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password("old-password-123"),
                role=UserRole.MEMBER,
            )
            session.add(member)
            await session.commit()
            member_id = member.id

        app = create_app()
        async with (
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as admin_client,
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as member_client,
        ):
            admin_login = await admin_client.post(
                "/auth/login",
                json={"username": "admin", "password": "password"},
            )
            member_login = await member_client.post(
                "/auth/login",
                json={
                    "username": "reset-target",
                    "password": "old-password-123",
                },
            )
            assert admin_login.status_code == 200
            assert member_login.status_code == 200
            admin_token = admin_login.json()["access_token"]
            member_token = member_login.json()["access_token"]

            response = await admin_client.put(
                f"/api/users/{member_id}/reset-password",
                json={"new_password": "reset-password-123"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            assert response.status_code == 200
            assert (
                await member_client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {member_token}"},
                )
            ).status_code == 401
            assert (await member_client.post("/auth/refresh")).status_code == 401
            assert (
                await admin_client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
            ).status_code == 200
            assert (await admin_client.post("/auth/refresh")).status_code == 200

        async with engine_mod._session_factory() as session:
            member = await session.get(User, member_id)
            assert member is not None
            assert member.credential_epoch == 2
            assert admin_user.credential_epoch == 1
            target_active = await session.scalar(
                select(func.count())
                .select_from(UserRefreshToken)
                .where(
                    UserRefreshToken.user_id == member_id,
                    UserRefreshToken.revoked_at.is_(None),
                )
            )
            admin_active = await session.scalar(
                select(func.count())
                .select_from(UserRefreshToken)
                .where(
                    UserRefreshToken.user_id == admin_user.id,
                    UserRefreshToken.revoked_at.is_(None),
                )
            )
            assert target_active == 0
            assert admin_active == 1


class TestBindingAPI:
    async def test_bind_and_unbind_user(self, client, admin_headers, admin_user):
        # Create instance
        resp = await client.post("/api/instances", json={
            "cluster_id": "pc-bind", "name": "Bind DB",
            "engine": "polardb_mysql", "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        }, headers=admin_headers)
        inst_id = resp.json()["id"]

        # Bind
        resp = await client.post(f"/api/instances/{inst_id}/bind-user", json={
            "user_id": admin_user.id
        }, headers=admin_headers)
        assert resp.status_code == 201

        # Unbind
        resp = await client.delete(f"/api/instances/{inst_id}/unbind-user/{admin_user.id}", headers=admin_headers)
        assert resp.status_code == 204

    async def test_bind_department(self, client, admin_headers):
        # Create department
        resp = await client.post("/api/departments", json={"name": "BindDept"}, headers=admin_headers)
        dept_id = resp.json()["id"]

        # Create instance
        resp = await client.post("/api/instances", json={
            "cluster_id": "pc-dept-bind", "name": "Dept DB",
            "engine": "polardb_mysql", "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        }, headers=admin_headers)
        inst_id = resp.json()["id"]

        # Bind
        resp = await client.post(f"/api/instances/{inst_id}/bind-department", json={
            "department_id": dept_id, "tenant_name": "test_tenant"
        }, headers=admin_headers)
        assert resp.status_code == 201


class TestUnauthorizedAccess:
    async def test_no_token_rejected(self, client):
        resp = await client.get("/api/users")
        assert resp.status_code in (401, 403)
