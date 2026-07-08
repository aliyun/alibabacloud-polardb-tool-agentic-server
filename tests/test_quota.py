import os

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.builtin import hash_password
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.quota_manager import (
    check_and_increment_quota, decrement_quota, reincrement_quota_for_retry,
)
from server.db import engine as engine_mod
from server.mcp.transport import reset_mcp
from server.models import Base, Instance, InstanceType, InstanceStatus, User, AuthProvider, UserRole, UserStatus
from server.models.instance import ProvisioningStep
from server.models.quota_counter import QuotaCounter


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    init_test_jwt_keys()
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


class TestQuotaCounterModel:
    async def test_create_global_counter(self, session: AsyncSession):
        counter = QuotaCounter(scope="global", current_count=0, max_limit=50)
        session.add(counter)
        await session.commit()
        await session.refresh(counter)
        assert counter.id is not None
        assert counter.scope == "global"
        assert counter.current_count == 0
        assert counter.max_limit == 50

    async def test_nullable_max_limit(self, session: AsyncSession):
        counter = QuotaCounter(scope="dept:abc", current_count=0, max_limit=None)
        session.add(counter)
        await session.commit()
        await session.refresh(counter)
        assert counter.max_limit is None

    async def test_unique_scope(self, session: AsyncSession):
        from sqlalchemy.exc import IntegrityError
        c1 = QuotaCounter(scope="global", current_count=0, max_limit=50)
        c2 = QuotaCounter(scope="global", current_count=0, max_limit=100)
        session.add(c1)
        await session.flush()
        session.add(c2)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# QuotaManager tests
# ---------------------------------------------------------------------------


async def _seed_global_counter(session, max_limit=50):
    counter = QuotaCounter(scope="global", current_count=0, max_limit=max_limit)
    session.add(counter)
    await session.commit()
    return counter


async def _seed_dept_counter(session, dept_id, max_limit=5):
    counter = QuotaCounter(scope=f"dept:{dept_id}", current_count=0, max_limit=max_limit)
    session.add(counter)
    await session.commit()
    return counter


async def _make_user(session, external_id="u1"):
    user = User(external_id=external_id, display_name="Test", auth_provider=AuthProvider.BUILTIN)
    session.add(user)
    await session.commit()
    return user


async def _make_instance(session, user_id, status=InstanceStatus.CREATING, quota_held=True):
    inst = Instance(
        cluster_id=f"pc-{user_id[:8]}", name="test", type=InstanceType.PERSONAL,
        status=status, owner_user_id=user_id, quota_held=quota_held,
        provisioning_step=ProvisioningStep.PENDING,
    )
    session.add(inst)
    await session.commit()
    return inst


class TestCheckAndIncrementQuota:
    async def test_global_quota_ok(self, session):
        await _seed_global_counter(session, max_limit=50)
        error = await check_and_increment_quota(session, department_id=None)
        assert error is None
        counter = (await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == "global")
        )).scalar_one()
        assert counter.current_count == 1

    async def test_global_quota_exceeded(self, session):
        c = await _seed_global_counter(session, max_limit=1)
        c.current_count = 1
        await session.commit()
        error = await check_and_increment_quota(session, department_id=None)
        assert error is not None
        assert error["error"] == "QUOTA_EXCEEDED"
        assert error["level"] == "global"

    async def test_global_null_max_limit_unlimited(self, session):
        await _seed_global_counter(session, max_limit=None)
        error = await check_and_increment_quota(session, department_id=None)
        assert error is None

    async def test_dept_quota_exceeded(self, session):
        await _seed_global_counter(session)
        c = await _seed_dept_counter(session, "dept1", max_limit=1)
        c.current_count = 1
        await session.commit()
        error = await check_and_increment_quota(session, department_id="dept1")
        assert error is not None
        assert error["level"] == "department"

    async def test_dept_null_max_limit_unlimited(self, session):
        await _seed_global_counter(session)
        await _seed_dept_counter(session, "dept2", max_limit=None)
        error = await check_and_increment_quota(session, department_id="dept2")
        assert error is None


class TestDecrementQuota:
    async def test_decrement_when_quota_held(self, session):
        await _seed_global_counter(session, max_limit=50)
        user = await _make_user(session)
        inst = await _make_instance(session, user.id, quota_held=True)
        counter = (await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == "global")
        )).scalar_one()
        counter.current_count = 1
        await session.commit()

        await decrement_quota(session, inst)
        await session.commit()

        counter = (await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == "global")
        )).scalar_one()
        assert counter.current_count == 0
        await session.refresh(inst)
        assert inst.quota_held is False

    async def test_decrement_idempotent_when_not_held(self, session):
        await _seed_global_counter(session, max_limit=50)
        user = await _make_user(session)
        inst = await _make_instance(session, user.id, quota_held=False)
        await decrement_quota(session, inst)
        await session.commit()
        counter = (await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == "global")
        )).scalar_one()
        assert counter.current_count == 0


class TestReincrementQuotaForRetry:
    async def test_reincrement_after_failed(self, session):
        await _seed_global_counter(session, max_limit=50)
        user = await _make_user(session)
        inst = await _make_instance(session, user.id, status=InstanceStatus.FAILED, quota_held=False)
        error = await reincrement_quota_for_retry(session, inst)
        assert error is None
        assert inst.quota_held is True

    async def test_reincrement_blocked_when_quota_full(self, session):
        c = await _seed_global_counter(session, max_limit=1)
        c.current_count = 1
        await session.commit()
        user = await _make_user(session)
        inst = await _make_instance(session, user.id, status=InstanceStatus.FAILED, quota_held=False)
        error = await reincrement_quota_for_retry(session, inst)
        assert error is not None
        assert error["error"] == "QUOTA_EXCEEDED"
        assert inst.quota_held is False


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------

_ADMIN_PASSWORD = "TestPass123"


@pytest.fixture
async def app_client():
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()
    os.environ["PAS_SERVER_DEV_MODE"] = "true"
    os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    os.environ["PAS_ADMIN_INITIAL_PASSWORD"] = _ADMIN_PASSWORD

    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)

    # Seed admin user and global quota counter
    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Administrator",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password(_ADMIN_PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        global_counter = QuotaCounter(scope="global", current_count=0, max_limit=50)
        session.add(global_counter)
        await session.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await e.dispose()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()


async def _login_admin(client: AsyncClient) -> dict:
    resp = await client.post("/auth/login", json={"username": "admin", "password": _ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.cookies


class TestQuotaAPI:
    async def test_get_quota_status(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.get("/api/quota/status", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "global" in data
        assert data["global"]["limit"] == 50
        assert data["global"]["current"] == 0

    async def test_update_global_limit(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.put("/api/quota/global", json={"max_limit": 100}, cookies=cookies)
        assert resp.status_code == 200
        resp = await app_client.get("/api/quota/status", cookies=cookies)
        assert resp.json()["global"]["limit"] == 100

    async def test_quota_requires_admin(self, app_client):
        resp = await app_client.get("/api/quota/status")
        assert resp.status_code == 401


class TestQuotaLimitValidation:
    async def test_reject_limit_below_current(self, session: AsyncSession):
        counter = QuotaCounter(scope="global", current_count=10, max_limit=50)
        session.add(counter)
        await session.commit()
        from server.api.quota import _validate_quota_limit
        with pytest.raises(ValueError, match="cannot be less than current usage"):
            _validate_quota_limit(new_limit=5, current_count=10)

    async def test_accept_limit_equal_to_current(self, session: AsyncSession):
        from server.api.quota import _validate_quota_limit
        _validate_quota_limit(new_limit=10, current_count=10)

    async def test_accept_limit_above_current(self, session: AsyncSession):
        from server.api.quota import _validate_quota_limit
        _validate_quota_limit(new_limit=20, current_count=10)
