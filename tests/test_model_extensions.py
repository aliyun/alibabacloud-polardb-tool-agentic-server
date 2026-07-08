import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import reset_config
from server.models import Base
from server.models.instance import Instance, InstanceType, InstanceStatus, ProvisioningStep
from server.models.user import User, AuthProvider, ProvisioningMode
from server.models.department import Department


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


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


class TestInstanceStatusExtensions:
    def test_new_statuses_exist(self):
        assert InstanceStatus.POOLED.value == "pooled"
        assert InstanceStatus.POOL_CREATING.value == "pool_creating"
        assert InstanceStatus.FAILED.value == "failed"

    async def test_provisioning_step_field(self, session):
        inst = Instance(
            cluster_id="pc-test-001", name="test", type=InstanceType.PERSONAL,
            status=InstanceStatus.CREATING,
            provisioning_step=ProvisioningStep.CLUSTER_READY,
            quota_held=True,
        )
        session.add(inst)
        await session.commit()
        await session.refresh(inst)
        assert inst.provisioning_step == ProvisioningStep.CLUSTER_READY
        assert inst.quota_held is True

    async def test_quota_held_default_false(self, session):
        inst = Instance(
            cluster_id="pc-test-002", name="test2", type=InstanceType.SHARED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(inst)
        await session.commit()
        await session.refresh(inst)
        assert inst.quota_held is False


class TestProvisioningMode:
    def test_enum_values(self):
        assert ProvisioningMode.DEDICATED.value == "dedicated"
        assert ProvisioningMode.MULTITENANT.value == "multitenant"

    async def test_user_provisioning_mode(self, session):
        user = User(
            external_id="pm-test", display_name="PM User",
            auth_provider=AuthProvider.BUILTIN,
            provisioning_mode=ProvisioningMode.DEDICATED,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        assert user.provisioning_mode == ProvisioningMode.DEDICATED

    async def test_user_provisioning_mode_nullable(self, session):
        user = User(
            external_id="pm-null", display_name="No Mode",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        assert user.provisioning_mode is None


class TestDepartmentMaxInstances:
    async def test_max_instances_field(self, session):
        dept = Department(name="Eng", max_instances=10)
        session.add(dept)
        await session.commit()
        await session.refresh(dept)
        assert dept.max_instances == 10

    async def test_max_instances_nullable(self, session):
        dept = Department(name="Design")
        session.add(dept)
        await session.commit()
        await session.refresh(dept)
        assert dept.max_instances is None
