# tests/test_orchestrator.py
"""Tests for server.core.orchestrator.provision_personal_instance.

Mocks the external PolarDB client only; internal modules (pool_manager,
quota_manager, provisioner) are exercised through the orchestrator.
"""
import base64
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import (
    MockPolarDBClient,
    reset_polardb_client,
    set_polardb_client,
)
from server.config import reset_config
from server.core.orchestrator import provision_personal_instance
from server.models import (
    AllocationMode,
    AuthProvider,
    Base,
    Instance,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    User,
)
from server.models.binding import UserDepartment
from server.models.department import Department
from server.models.instance import ProvisioningStep
from server.models.quota_counter import QuotaCounter
from server.models.user import ProvisioningMode


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    reset_polardb_client()
    key = base64.b64encode(os.urandom(32)).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key
    yield
    reset_config()
    reset_polardb_client()
    os.environ.pop("PAS_ENCRYPTION_KEY", None)


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


async def _seed(session, *, max_limit: int = 50):
    counter = QuotaCounter(scope="global", current_count=0, max_limit=max_limit)
    session.add(counter)
    await session.commit()


async def _make_user(session, eid="orch-user"):
    """Create a multitenant user (exercises pool path)."""
    user = User(
        external_id=eid, display_name="Test",
        auth_provider=AuthProvider.BUILTIN,
        provisioning_mode=ProvisioningMode.MULTITENANT,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _make_department(session, name="TestDept"):
    dept = Department(name=name)
    session.add(dept)
    await session.commit()
    await session.refresh(dept)
    return dept


async def _make_dedicated_user_with_dept(session, dept, eid="ded-user"):
    user = User(
        external_id=eid, display_name="Dedicated",
        auth_provider=AuthProvider.BUILTIN,
        provisioning_mode=ProvisioningMode.DEDICATED,
    )
    session.add(user)
    await session.flush()
    membership = UserDepartment(
        user_id=user.id, department_id=dept.id, is_primary=True,
    )
    session.add(membership)
    await session.commit()
    await session.refresh(user)
    return user


class TestProvisionPersonalInstance:
    async def test_pool_miss_creates_placeholder_and_launches_task(
        self, session, session_factory
    ):
        await _seed(session)
        user = await _make_user(session)
        set_polardb_client(MockPolarDBClient())

        bg_tasks: set = set()
        result = await provision_personal_instance(
            user, session, session_factory, bg_tasks,
        )

        assert isinstance(result, Instance)
        assert result.status == InstanceStatus.CREATING
        assert result.owner_user_id == user.id
        assert result.engine == InstanceEngine.POLARDB_MYSQL
        assert result.topology == InstanceTopology.SINGLE_TENANT
        assert result.allocation_mode == AllocationMode.AUTO_PROVISIONED
        assert result.cluster_id.startswith("pending-")
        assert len(bg_tasks) == 1

    async def test_pool_hit_claims_pooled_instance(
        self, session, session_factory
    ):
        await _seed(session)
        user = await _make_user(session)
        set_polardb_client(MockPolarDBClient())

        pooled = Instance(
            cluster_id="pc-pool-001",
            name="pool-001",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.POOLED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(pooled)
        await session.commit()

        bg_tasks: set = set()
        result = await provision_personal_instance(
            user, session, session_factory, bg_tasks,
        )

        assert isinstance(result, Instance)
        assert result.id == pooled.id
        assert result.owner_user_id == user.id
        assert result.status == InstanceStatus.ACTIVE
        assert result.allocation_mode == AllocationMode.POOLED
        assert result.provisioning_step == ProvisioningStep.CLUSTER_READY
        assert result.quota_held is True
        assert len(bg_tasks) == 1

    async def test_quota_exceeded_returns_error_dict(
        self, session, session_factory
    ):
        await _seed(session, max_limit=0)
        user = await _make_user(session)
        set_polardb_client(MockPolarDBClient())

        bg_tasks: set = set()
        result = await provision_personal_instance(
            user, session, session_factory, bg_tasks,
        )

        assert isinstance(result, dict)
        assert result["error"] == "QUOTA_EXCEEDED"
        assert result["level"] == "global"
        assert len(bg_tasks) == 0


class TestDedicatedProvisionBranch:
    async def test_dedicated_user_skips_pool(self, session, session_factory):
        """Dedicated user with department should NOT claim POOLED instances."""
        await _seed(session)
        dept = await _make_department(session)
        user = await _make_dedicated_user_with_dept(session, dept)
        set_polardb_client(MockPolarDBClient())

        pooled = Instance(
            cluster_id="pc-pool-ded-001", name="pool-ded",
            allocation_mode=AllocationMode.POOLED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(pooled)
        await session.commit()

        bg_tasks: set = set()
        result = await provision_personal_instance(
            user, session, session_factory, bg_tasks,
        )

        assert isinstance(result, Instance)
        assert result.cluster_id.startswith("pending-")
        assert result.id != pooled.id
        assert len(bg_tasks) == 1

    async def test_dedicated_user_no_department_returns_error(
        self, session, session_factory
    ):
        """Dedicated user without primary department gets error."""
        await _seed(session)
        user = User(
            external_id="ded-no-dept", display_name="NoDept",
            auth_provider=AuthProvider.BUILTIN,
            provisioning_mode=ProvisioningMode.DEDICATED,
        )
        session.add(user)
        await session.commit()
        set_polardb_client(MockPolarDBClient())

        bg_tasks: set = set()
        result = await provision_personal_instance(
            user, session, session_factory, bg_tasks,
        )

        assert isinstance(result, dict)
        assert result["error"] == "NO_PRIMARY_DEPARTMENT"
        assert len(bg_tasks) == 0

    async def test_dedicated_user_quota_exceeded(self, session, session_factory):
        """Dedicated user still gets quota check."""
        await _seed(session, max_limit=0)
        dept = await _make_department(session)
        user = await _make_dedicated_user_with_dept(session, dept, eid="ded-quota")
        set_polardb_client(MockPolarDBClient())

        bg_tasks: set = set()
        result = await provision_personal_instance(
            user, session, session_factory, bg_tasks,
        )

        assert isinstance(result, dict)
        assert result["error"] == "QUOTA_EXCEEDED"
        assert len(bg_tasks) == 0
