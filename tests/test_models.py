import pytest
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.models import (
    Base, User, Department, Instance, UserDepartment,
    UserInstanceBinding, DepartmentInstanceBinding, InstanceCredential, AuditLog,
    AuthProvider, UserRole, UserStatus, InstanceStatus, InstanceTopology,
    AllocationMode, CredentialCapability, CredentialPurpose, Permission, AuditStatus,
    OAuthRegisteredClient, OAuthPendingAuth, UserExternalIdentity,
)


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(e.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


class TestUserModel:
    async def test_create_user(self, session: AsyncSession):
        user = User(
            external_id="test-user",
            display_name="Test User",
            email="test@example.com",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        assert user.id is not None
        assert user.role == UserRole.MEMBER
        assert user.status == UserStatus.ACTIVE
        assert user.auth_provider == AuthProvider.BUILTIN

    async def test_unique_external_id(self, session: AsyncSession):
        u1 = User(external_id="dup", display_name="A")
        u2 = User(external_id="dup", display_name="B")
        session.add(u1)
        await session.commit()
        session.add(u2)
        with pytest.raises(Exception):
            await session.commit()


class TestDepartmentModel:
    async def test_create_department(self, session: AsyncSession):
        dept = Department(name="Engineering")
        session.add(dept)
        await session.commit()
        await session.refresh(dept)
        assert dept.id is not None
        assert dept.name == "Engineering"


class TestInstanceModel:
    async def test_create_instance(self, session: AsyncSession):
        inst = Instance(
            cluster_id="pc-test-001",
            name="Test Instance",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
        )
        session.add(inst)
        await session.commit()
        await session.refresh(inst)
        assert inst.status == InstanceStatus.ACTIVE

    async def test_unique_cluster_id(self, session: AsyncSession):
        i1 = Instance(cluster_id="pc-dup", name="A", topology=InstanceTopology.SINGLE_TENANT)
        i2 = Instance(cluster_id="pc-dup", name="B", topology=InstanceTopology.SINGLE_TENANT)
        session.add(i1)
        await session.commit()
        session.add(i2)
        with pytest.raises(Exception):
            await session.commit()


class TestBindingModels:
    async def test_user_department_binding(self, session: AsyncSession):
        user = User(external_id="u1", display_name="U1")
        dept = Department(name="Eng")
        session.add_all([user, dept])
        await session.commit()

        ud = UserDepartment(user_id=user.id, department_id=dept.id, is_primary=True)
        session.add(ud)
        await session.commit()
        await session.refresh(ud)
        assert ud.is_primary is True

    async def test_unique_user_department(self, session: AsyncSession):
        user = User(external_id="u2", display_name="U2")
        dept = Department(name="Sales")
        session.add_all([user, dept])
        await session.commit()

        ud1 = UserDepartment(user_id=user.id, department_id=dept.id)
        session.add(ud1)
        await session.commit()
        ud2 = UserDepartment(user_id=user.id, department_id=dept.id)
        session.add(ud2)
        with pytest.raises(Exception):
            await session.commit()

    async def test_user_instance_binding(self, session: AsyncSession):
        user = User(external_id="u3", display_name="U3")
        inst = Instance(cluster_id="pc-003", name="I3", topology=InstanceTopology.SINGLE_TENANT)
        session.add_all([user, inst])
        await session.commit()

        binding = UserInstanceBinding(
            user_id=user.id, instance_id=inst.id, permission=Permission.READONLY
        )
        session.add(binding)
        await session.commit()
        assert binding.permission == Permission.READONLY

    async def test_department_instance_binding(self, session: AsyncSession):
        dept = Department(name="Ops")
        inst = Instance(cluster_id="pc-004", name="I4", topology=InstanceTopology.SINGLE_TENANT)
        session.add_all([dept, inst])
        await session.commit()

        binding = DepartmentInstanceBinding(
            department_id=dept.id, instance_id=inst.id, tenant_name="ops_tenant"
        )
        session.add(binding)
        await session.commit()
        assert binding.tenant_name == "ops_tenant"


class TestInstanceCredentialModel:
    async def test_create_instance_credential(self, session: AsyncSession):
        user = User(external_id="u4", display_name="U4")
        inst = Instance(cluster_id="pc-005", name="I5", topology=InstanceTopology.SINGLE_TENANT)
        session.add_all([user, inst])
        await session.commit()

        credential = InstanceCredential(
            instance_id=inst.id,
            name="pas_u4",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext="encrypted_user",
            password_ciphertext="encrypted_pw",
            created_by_user_id=user.id,
        )
        session.add(credential)
        await session.commit()
        assert credential.name == "pas_u4"

    async def test_unique_instance_credential_name(self, session: AsyncSession):
        user = User(external_id="u5", display_name="U5")
        inst = Instance(cluster_id="pc-006", name="I6", topology=InstanceTopology.SINGLE_TENANT)
        session.add_all([user, inst])
        await session.commit()

        c1 = InstanceCredential(instance_id=inst.id, name="a1", purpose=CredentialPurpose.DIRECT_ACCESS,
                                capability=CredentialCapability.READWRITE, username_ciphertext="u1",
                                password_ciphertext="pw1", created_by_user_id=user.id)
        session.add(c1)
        await session.commit()
        c2 = InstanceCredential(instance_id=inst.id, name="a1", purpose=CredentialPurpose.DIRECT_ACCESS,
                                capability=CredentialCapability.READWRITE, username_ciphertext="u2",
                                password_ciphertext="pw2", created_by_user_id=user.id)
        session.add(c2)
        with pytest.raises(Exception):
            await session.commit()


class TestAuditLogModel:
    async def test_create_audit_log(self, session: AsyncSession):
        user = User(external_id="u6", display_name="U6")
        session.add(user)
        await session.commit()

        log = AuditLog(
            actor_user_id=user.id,
            action="run_sql",
            status=AuditStatus.SUCCESS,
            duration_ms=42,
            metadata_json='{"sql_text": "SELECT 1", "row_count": 1}',
        )
        session.add(log)
        await session.commit()
        assert log.duration_ms == 42


class TestUserDefaultInstance:
    async def test_default_instance_id_nullable(self, session: AsyncSession):
        user = User(external_id="def-test", display_name="Default Test")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        assert user.default_instance_id is None

    async def test_set_default_instance_id(self, session: AsyncSession):
        user = User(external_id="def-set", display_name="Set Default")
        inst = Instance(cluster_id="pc-def-001", name="Default Inst", topology=InstanceTopology.SINGLE_TENANT)
        session.add_all([user, inst])
        await session.commit()

        user.default_instance_id = inst.id
        await session.commit()
        await session.refresh(user)
        assert user.default_instance_id == inst.id

    async def test_fk_set_null_on_instance_delete(self, session: AsyncSession):
        user = User(external_id="def-del", display_name="Delete Test")
        inst = Instance(cluster_id="pc-def-002", name="Deletable", topology=InstanceTopology.SINGLE_TENANT,
                        allocation_mode=AllocationMode.AUTO_PROVISIONED)
        session.add_all([user, inst])
        await session.commit()

        user.default_instance_id = inst.id
        await session.commit()

        await session.delete(inst)
        await session.commit()
        await session.refresh(user)
        assert user.default_instance_id is None


class TestOAuthRegisteredClient:
    async def test_create_client(self, session: AsyncSession):
        client = OAuthRegisteredClient(
            client_id="test-client-001",
            redirect_uris='["http://localhost:3000/callback"]',
            grant_types='["authorization_code"]',
            response_types='["code"]',
            client_name="Test App",
            scope="read write",
            token_endpoint_auth_method="none",
        )
        session.add(client)
        await session.commit()
        await session.refresh(client)
        assert client.client_id == "test-client-001"
        assert client.client_name == "Test App"
        assert client.client_secret_enc is None
        assert client.created_at is not None


class TestOAuthPendingAuth:
    async def test_create_with_auto_session_id(self, session: AsyncSession):
        from datetime import datetime, timezone, timedelta

        pending = OAuthPendingAuth(
            client_id="test-client-001",
            redirect_uri="http://localhost:3000/callback",
            code_challenge="abc123challenge",
            code_challenge_method="S256",
            scopes='["read"]',
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        session.add(pending)
        await session.commit()
        await session.refresh(pending)
        assert pending.session_id is not None
        assert len(pending.session_id) == 36  # UUID format
        assert pending.client_id == "test-client-001"
        assert pending.state is None
        assert pending.idp_state is None


class TestUserExternalIdentity:
    async def test_unique_idp_subject(self, session: AsyncSession):
        user1 = User(external_id="ext-u1", display_name="User 1")
        user2 = User(external_id="ext-u2", display_name="User 2")
        session.add_all([user1, user2])
        await session.commit()

        id1 = UserExternalIdentity(
            user_id=user1.id,
            identity_provider="google",
            external_subject="google-sub-123",
        )
        session.add(id1)
        await session.commit()

        id2 = UserExternalIdentity(
            user_id=user2.id,
            identity_provider="google",
            external_subject="google-sub-123",
        )
        session.add(id2)
        with pytest.raises(Exception):
            await session.commit()


class TestAllTablesCreated:
    async def test_all_tables_exist(self, engine):
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_table_names()
            )
        expected = {"users", "departments", "instances", "user_departments",
                    "user_instance_bindings", "department_instance_bindings",
                    "instance_credentials", "audit_logs",
                    "oauth_registered_clients", "oauth_authorization_codes",
                    "oauth_refresh_tokens", "oauth_denied_jtis",
                    "oauth_pending_auths", "user_external_identities"}
        assert expected.issubset(set(tables))
