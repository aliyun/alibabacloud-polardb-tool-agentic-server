import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client
from server.config import reset_config
from server.core import user_manager, department_manager, instance_manager, binding_manager
from server.models import (
    Base, User, Department, Instance, AuthProvider, UserRole, UserStatus,
    InstanceTopology,
)


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    set_polardb_client(MockPolarDBClient())
    yield
    reset_config()
    reset_polardb_client()


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


@pytest.fixture
async def sample_user(session: AsyncSession) -> User:
    user = User(external_id="u1", display_name="User 1", email="u1@test.com", auth_provider=AuthProvider.BUILTIN)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
async def sample_dept(session: AsyncSession) -> Department:
    return await department_manager.create_department(session, "Engineering", "Eng team")


@pytest.fixture
async def sample_instance(session: AsyncSession) -> Instance:
    return await instance_manager.register_instance(
        session, cluster_id="pc-001", name="Test Instance",
        topology=InstanceTopology.SINGLE_TENANT,
    )


class TestUserManager:
    async def test_list_users(self, session, sample_user):
        users, total = await user_manager.list_users(session)
        assert total >= 1
        assert any(u.id == sample_user.id for u in users)

    async def test_search_users(self, session, sample_user):
        users, total = await user_manager.list_users(session, search="User 1")
        assert total == 1

    async def test_update_role(self, session, sample_user):
        updated = await user_manager.update_user_role(session, sample_user.id, UserRole.ADMIN)
        assert updated.role == UserRole.ADMIN

    async def test_disable_user(self, session, sample_user):
        updated = await user_manager.set_user_status(session, sample_user.id, UserStatus.DISABLED)
        assert updated.status == UserStatus.DISABLED

    async def test_update_departments(self, session, sample_user, sample_dept):
        updated = await user_manager.update_user_departments(
            session, sample_user.id, [sample_dept.id], primary_department_id=sample_dept.id
        )
        assert len(updated.department_memberships) == 1
        assert updated.department_memberships[0].is_primary is True


class TestDepartmentManager:
    async def test_create_department(self, session):
        dept = await department_manager.create_department(session, "Sales")
        assert dept.name == "Sales"

    async def test_list_departments(self, session, sample_dept):
        depts = await department_manager.list_departments(session)
        assert len(depts) >= 1

    async def test_update_department(self, session, sample_dept):
        updated = await department_manager.update_department(session, sample_dept.id, name="Eng Updated")
        assert updated.name == "Eng Updated"

    async def test_delete_empty_department(self, session):
        dept = await department_manager.create_department(session, "Empty Dept")
        await department_manager.delete_department(session, dept.id)
        assert await department_manager.get_department(session, dept.id) is None

    async def test_delete_department_with_users_fails(self, session, sample_user, sample_dept):
        await user_manager.update_user_departments(session, sample_user.id, [sample_dept.id])
        with pytest.raises(ValueError, match="active user"):
            await department_manager.delete_department(session, sample_dept.id)

    async def test_list_department_users(self, session, sample_user, sample_dept):
        await user_manager.update_user_departments(session, sample_user.id, [sample_dept.id])
        users = await department_manager.list_department_users(session, sample_dept.id)
        assert len(users) == 1


class TestInstanceManager:
    async def test_register_instance(self, session, sample_instance):
        assert sample_instance.cluster_id == "pc-001"

    async def test_list_instances(self, session, sample_instance):
        instances = await instance_manager.list_instances(session)
        assert len(instances) >= 1

    async def test_duplicate_cluster_id_fails(self, session, sample_instance):
        with pytest.raises(ValueError, match="already registered"):
            await instance_manager.register_instance(
                session, cluster_id="pc-001", name="Dup",
                topology=InstanceTopology.SINGLE_TENANT,
            )

    async def test_remove_instance(self, session):
        inst = await instance_manager.register_instance(
            session, cluster_id="pc-remove", name="Remove Me",
            topology=InstanceTopology.SINGLE_TENANT,
        )
        await instance_manager.remove_instance(session, inst.id)
        assert await instance_manager.get_instance(session, inst.id) is None


class TestBindingManager:
    async def test_bind_user_creates_account(self, session, sample_user, sample_instance):
        key = os.urandom(32)
        binding = await binding_manager.bind_user_to_instance(
            session, sample_user.id, sample_instance.id, encryption_key=key
        )
        assert binding.credential_id is not None

    async def test_duplicate_binding_fails(self, session, sample_user, sample_instance):
        key = os.urandom(32)
        await binding_manager.bind_user_to_instance(
            session, sample_user.id, sample_instance.id, encryption_key=key
        )
        with pytest.raises(ValueError, match="already bound"):
            await binding_manager.bind_user_to_instance(
                session, sample_user.id, sample_instance.id, encryption_key=key
            )

    async def test_unbind_user(self, session, sample_user, sample_instance):
        key = os.urandom(32)
        await binding_manager.bind_user_to_instance(
            session, sample_user.id, sample_instance.id, encryption_key=key
        )
        await binding_manager.unbind_user_from_instance(session, sample_user.id, sample_instance.id)

    async def test_bind_department(self, session, sample_dept, sample_instance):
        binding = await binding_manager.bind_department_to_instance(
            session, sample_dept.id, sample_instance.id, tenant_name="eng_tenant"
        )
        assert binding.tenant_name == "eng_tenant"

    async def test_accessible_instances_personal(self, session, sample_user, sample_instance):
        key = os.urandom(32)
        await binding_manager.bind_user_to_instance(
            session, sample_user.id, sample_instance.id, encryption_key=key
        )
        accessible = await binding_manager.get_accessible_instances(session, sample_user)
        assert len(accessible) == 1
        assert accessible[0]["access_type"] == "personal"

    async def test_accessible_instances_department(self, session, sample_user, sample_dept, sample_instance):
        # Add user to department
        await user_manager.update_user_departments(session, sample_user.id, [sample_dept.id])
        # Bind department to instance
        await binding_manager.bind_department_to_instance(session, sample_dept.id, sample_instance.id)
        # Check access
        accessible = await binding_manager.get_accessible_instances(session, sample_user)
        assert len(accessible) == 1
        assert accessible[0]["access_type"] == "department"
