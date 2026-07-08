# tests/test_resolve_target.py
import base64
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import reset_config
from server.models import Base, Instance, InstanceType, InstanceStatus, User, AuthProvider
from server.models.instance import ProvisioningStep
from server.models.quota_counter import QuotaCounter
from server.models.system_setting import SystemSetting, SETTINGS_SCHEMA
from server.mcp.tools import resolve_target_instance
from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client


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


async def _seed(session):
    counter = QuotaCounter(scope="global", current_count=0, max_limit=50)
    session.add(counter)
    for key, sd in SETTINGS_SCHEMA.items():
        session.add(SystemSetting(key=key, value=sd.default, description=sd.description))
    await session.commit()


async def _make_user(session, eid="resolve-user"):
    """Create a dedicated user with a department (exercises auto-provision path)."""
    from server.models.binding import UserDepartment
    from server.models.department import Department
    from server.models.user import ProvisioningMode

    dept = Department(name="TestDept")
    session.add(dept)
    await session.flush()

    user = User(
        external_id=eid, display_name="Test",
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


class TestResolveTargetInstanceAutoProvision:
    async def test_creating_returns_instance_creating(self, session, session_factory):
        await _seed(session)
        user = await _make_user(session)
        inst = Instance(
            cluster_id="pc-creating", name="creating", type=InstanceType.PERSONAL,
            status=InstanceStatus.CREATING, owner_user_id=user.id,
            provisioning_step=ProvisioningStep.ACCOUNT_CREATED, quota_held=True,
        )
        session.add(inst)
        await session.commit()

        result = await resolve_target_instance(user, session, session_factory=session_factory,
                                                background_tasks=set())
        assert isinstance(result, dict)
        assert result["isError"] is True
        assert "INSTANCE_CREATING" in result["content"][0]["text"]

    async def test_failed_with_no_accessible_returns_failed(self, session, session_factory):
        await _seed(session)
        user = await _make_user(session)
        inst = Instance(
            cluster_id="pc-failed", name="failed", type=InstanceType.PERSONAL,
            status=InstanceStatus.FAILED, owner_user_id=user.id,
            provisioning_step=ProvisioningStep.PASSWORD_STORED, quota_held=False,
        )
        session.add(inst)
        await session.commit()

        result = await resolve_target_instance(user, session, session_factory=session_factory,
                                                background_tasks=set())
        assert isinstance(result, dict)
        assert "INSTANCE_PROVISION_FAILED" in result["content"][0]["text"]

    async def test_no_instance_triggers_auto_provision(self, session, session_factory):
        await _seed(session)
        user = await _make_user(session)
        mock = MockPolarDBClient()
        set_polardb_client(mock)

        bg_tasks: set = set()
        result = await resolve_target_instance(user, session, session_factory=session_factory,
                                                background_tasks=bg_tasks)
        assert isinstance(result, tuple)
        instance, accessible = result
        assert isinstance(instance, Instance)
        assert instance.status == InstanceStatus.CREATING
        assert instance.owner_user_id == user.id
