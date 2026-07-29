# tests/test_pool.py
import base64
import os

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.builtin import hash_password
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.db.engine import reset_engine
from server.mcp.transport import reset_mcp
from server.models import (
    AllocationMode,
    AuthProvider,
    Base,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    User,
    UserRole,
    UserStatus,
)
from server.models.instance import ProvisioningStep
from server.models.quota_counter import QuotaCounter
from server.aliyun.polardb_client import reset_polardb_client


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    init_test_jwt_keys()
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
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session):
    counter = QuotaCounter(scope="global", current_count=0, max_limit=50)
    session.add(counter)
    await session.commit()


async def _make_user(session, eid="pool-user"):
    from server.models.user import ProvisioningMode
    user = User(
        external_id=eid, display_name="Pool User",
        auth_provider=AuthProvider.BUILTIN,
        provisioning_mode=ProvisioningMode.MULTITENANT,
    )
    session.add(user)
    await session.commit()
    return user


async def _make_pooled(session, cluster_id="pc-pooled-001"):
    inst = Instance(
        cluster_id=cluster_id,
        name="pool-inst",
        engine=InstanceEngine.POLARDB_MYSQL,
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.POOLED,
        status=InstanceStatus.ACTIVE,
    )
    session.add(inst)
    await session.commit()
    return inst


class TestAllocateFromPool:
    async def test_pool_assignment_preserves_pooled_origin(self, session):
        from server.core.pool_manager import allocate_from_pool
        await _seed(session)
        user = await _make_user(session)
        pooled = await _make_pooled(session)

        result = await allocate_from_pool(user.id, None, session)
        assert isinstance(result, Instance)
        assert result.id == pooled.id
        assert result.allocation_mode == AllocationMode.POOLED
        assert result.status == InstanceStatus.ACTIVE
        assert result.owner_user_id == user.id
        assert result.provisioning_step == ProvisioningStep.CLUSTER_READY
        assert result.quota_held is True

    async def test_pool_empty_fallback(self, session):
        from server.core.pool_manager import allocate_from_pool
        await _seed(session)
        user = await _make_user(session)

        result = await allocate_from_pool(user.id, None, session)
        assert isinstance(result, Instance)
        assert result.cluster_id.startswith("pending-")
        assert result.status == InstanceStatus.CREATING
        assert result.provisioning_step == ProvisioningStep.PENDING

    async def test_quota_exceeded(self, session):
        from server.core.pool_manager import allocate_from_pool
        await _seed(session)
        counter = (await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == "global")
        )).scalar_one()
        counter.current_count = 50
        await session.commit()

        user = await _make_user(session)
        result = await allocate_from_pool(user.id, None, session)
        assert isinstance(result, dict)
        assert result["error"] == "QUOTA_EXCEEDED"


class TestActiveAutoProvisionedUniqueIndex:
    """Regression: the active-user index only constrains auto-provisioned rows."""

    async def test_duplicate_creating_blocked(self, session):
        from sqlalchemy.exc import IntegrityError

        user = await _make_user(session)
        session.add(Instance(
            cluster_id="c-dup-1", name="i1",
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.CREATING, owner_user_id=user.id,
        ))
        await session.commit()

        session.add(Instance(
            cluster_id="c-dup-2", name="i2",
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.CREATING, owner_user_id=user.id,
        ))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_failed_does_not_block_new_creating(self, session):
        user = await _make_user(session)
        session.add(Instance(
            cluster_id="c-failed", name="failed",
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.FAILED, owner_user_id=user.id,
        ))
        await session.commit()

        session.add(Instance(
            cluster_id="c-new", name="new",
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.CREATING, owner_user_id=user.id,
        ))
        await session.commit()

    async def test_pooled_instances_unconstrained(self, session):
        user = await _make_user(session)
        session.add(Instance(
            cluster_id="s1", name="s1", allocation_mode=AllocationMode.POOLED,
            status=InstanceStatus.ACTIVE, owner_user_id=user.id,
        ))
        session.add(Instance(
            cluster_id="s2", name="s2", allocation_mode=AllocationMode.POOLED,
            status=InstanceStatus.ACTIVE, owner_user_id=user.id,
        ))
        await session.commit()


class TestAllocateRace:
    """allocate_from_pool must recover gracefully when the partial unique index fires."""

    async def test_pool_hit_keeps_pooled_origin_with_existing_auto_instance(self, session):
        from server.core.pool_manager import allocate_from_pool
        await _seed(session)
        user = await _make_user(session)
        pooled = await _make_pooled(session)

        existing = Instance(
            cluster_id="c-existing", name="existing",
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.CREATING, owner_user_id=user.id,
            provisioning_step=ProvisioningStep.CLUSTER_READY, quota_held=True,
        )
        session.add(existing)
        await session.commit()

        result = await allocate_from_pool(user.id, None, session)
        assert isinstance(result, Instance)
        assert result.id == pooled.id
        assert result.allocation_mode == AllocationMode.POOLED
        assert result.status == InstanceStatus.ACTIVE
        assert result.owner_user_id == user.id

    async def test_fallback_returns_existing_on_conflict(self, session):
        from server.core.pool_manager import allocate_from_pool
        await _seed(session)
        user = await _make_user(session)

        existing = Instance(
            cluster_id="c-existing-fb", name="existing",
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.CREATING, owner_user_id=user.id,
            provisioning_step=ProvisioningStep.PENDING, quota_held=True,
        )
        session.add(existing)
        await session.commit()

        result = await allocate_from_pool(user.id, None, session)
        assert isinstance(result, Instance)
        assert result.id == existing.id


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------

_ADMIN_PASSWORD = "TestPass123"


@pytest.fixture
async def app_client():
    reset_config()
    reset_engine()
    reset_mcp()
    os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

    # Create in-memory engine and tables, then inject into engine module
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)

    # Seed admin user (lifespan may not run reliably under ASGITransport)
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
        await session.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await e.dispose()
    reset_config()
    reset_engine()
    reset_mcp()


async def _login_admin(client: AsyncClient) -> dict:
    resp = await client.post("/auth/login", json={"username": "admin", "password": _ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.cookies


class TestPoolAPI:
    async def test_get_pool_status(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.get("/api/pool/status", cookies=cookies)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "target" in data
        assert "available" in data
        assert "pool_creating" in data
        assert "failed" in data
        assert "network_ready" in data
        assert data["target"] == 0
        assert data["available"] == 0

    async def test_get_pool_status_requires_admin(self, app_client):
        resp = await app_client.get("/api/pool/status")
        assert resp.status_code == 401

    async def test_list_pool_instances_empty(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.get("/api/pool/instances", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_replenish_when_target_zero(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.post("/api/pool/replenish", cookies=cookies)
        assert resp.status_code == 400


def _placeholder(cluster_id: str, status: InstanceStatus, age_seconds: int = 0):
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return Instance(
        cluster_id=cluster_id,
        name="pool-pending",
        engine=InstanceEngine.POLARDB_MYSQL,
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.POOLED,
        status=status,
        created_at=stamp,
        updated_at=stamp,
    )


class TestStalePlaceholderCleanup:
    async def test_replenish_skips_when_vpc_network_is_missing(
        self, session_factory
    ):
        from server import config as config_module
        from server.config import AppConfig
        from server.core.pool_manager import _replenish_once

        config_module._config = AppConfig(
            polardb={
                "resource_pool": {
                    "target_size": 1,
                    "region_id": "cn-test",
                    "zone_id": "cn-test-a",
                }
            }
        )

        await _replenish_once(session_factory, set())

        async with session_factory() as session:
            instances = (
                await session.execute(select(Instance))
            ).scalars().all()
        assert instances == []

    async def test_replenish_removes_stale_placeholders(self, session_factory):
        from server import config as config_module
        from server.config import AppConfig
        from server.core.pool_manager import _replenish_once

        config_module._config = AppConfig(
            polardb={"resource_pool": {"target_size": 0}}
        )

        async with session_factory() as s:
            s.add(_placeholder("pool-pending-stale-creating", InstanceStatus.CREATING, 3600))
            s.add(_placeholder("pool-pending-stale-failed", InstanceStatus.FAILED, 3600))
            s.add(_placeholder("pool-pending-fresh", InstanceStatus.CREATING, 0))
            await s.commit()

        await _replenish_once(session_factory, set())

        async with session_factory() as s:
            remaining = {
                row.cluster_id
                for row in (await s.execute(select(Instance))).scalars().all()
            }
        assert remaining == {"pool-pending-fresh"}

    async def test_replenish_commits_stale_cleanup_when_target_is_satisfied(
        self, session_factory
    ):
        from server import config as config_module
        from server.config import AppConfig
        from server.core.pool_manager import _replenish_once

        config_module._config = AppConfig(
            polardb={
                "resource_pool": {
                    "target_size": 1,
                    "region_id": "cn-test",
                    "zone_id": "cn-test-a",
                }
            }
        )

        async with session_factory() as s:
            s.add(_placeholder("pool-active", InstanceStatus.ACTIVE))
            s.add(_placeholder("pool-pending-stale", InstanceStatus.CREATING, 3600))
            await s.commit()

        await _replenish_once(session_factory, set())

        async with session_factory() as s:
            remaining = {
                row.cluster_id
                for row in (await s.execute(select(Instance))).scalars().all()
            }
        assert remaining == {"pool-active"}


class TestRemovePoolInstance:
    async def _add(self, inst) -> str:
        async with engine_mod._session_factory() as s:
            s.add(inst)
            await s.commit()
            return inst.id

    async def test_remove_failed_instance(self, app_client):
        cookies = await _login_admin(app_client)
        iid = await self._add(
            _placeholder("pool-pending-failed-1", InstanceStatus.FAILED)
        )
        resp = await app_client.delete(f"/api/pool/instances/{iid}", cookies=cookies)
        assert resp.status_code == 204

    async def test_remove_fresh_creating_placeholder_rejected(self, app_client):
        cookies = await _login_admin(app_client)
        iid = await self._add(
            _placeholder("pool-pending-fresh-1", InstanceStatus.CREATING)
        )
        resp = await app_client.delete(f"/api/pool/instances/{iid}", cookies=cookies)
        assert resp.status_code == 400

    async def test_remove_stale_creating_placeholder(self, app_client):
        cookies = await _login_admin(app_client)
        iid = await self._add(
            _placeholder("pool-pending-stale-1", InstanceStatus.CREATING, 3600)
        )
        resp = await app_client.delete(f"/api/pool/instances/{iid}", cookies=cookies)
        assert resp.status_code == 204


class TestDeleteFailedInstance:
    """DELETE /api/instances/{id}/failed must clean up dependent rows and FK refs."""

    async def test_cascade_cleanup(self, app_client):
        from server.models.binding import (
            DepartmentInstanceBinding, UserInstanceBinding,
        )
        from server.models.department import Department

        cookies = await _login_admin(app_client)

        async with engine_mod._session_factory() as s:
            owner = User(
                external_id="owner1", display_name="Owner",
                auth_provider=AuthProvider.BUILTIN,
                role=UserRole.MEMBER, status=UserStatus.ACTIVE,
            )
            dept = Department(name="d1")
            s.add_all([owner, dept])
            await s.flush()

            failed = Instance(
                cluster_id="c-failed-cascade", name="failed",
                allocation_mode=AllocationMode.AUTO_PROVISIONED,
                status=InstanceStatus.FAILED,
                owner_user_id=owner.id, quota_held=False,
            )
            s.add(failed)
            await s.flush()

            credential = InstanceCredential(
                instance_id=failed.id,
                name="agentic",
                purpose=CredentialPurpose.DIRECT_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext="x",
                password_ciphertext="y",
                database_name="agentic",
                created_by_user_id=owner.id,
            )
            s.add(credential)
            await s.flush()
            ub = UserInstanceBinding(
                user_id=owner.id,
                instance_id=failed.id,
                credential_id=credential.id,
            )
            db = DepartmentInstanceBinding(department_id=dept.id, instance_id=failed.id)
            owner.default_instance_id = failed.id
            s.add_all([ub, db])
            await s.commit()
            instance_id = failed.id
            owner_id = owner.id

        resp = await app_client.delete(
            f"/api/instances/{instance_id}/failed", cookies=cookies,
        )
        assert resp.status_code == 204

        async with engine_mod._session_factory() as s:
            assert (await s.get(Instance, instance_id)) is None
            remaining_credentials = (await s.execute(
                select(InstanceCredential).where(
                    InstanceCredential.instance_id == instance_id
                )
            )).scalars().all()
            assert remaining_credentials == []
            remaining_ub = (await s.execute(
                select(UserInstanceBinding).where(
                    UserInstanceBinding.instance_id == instance_id
                )
            )).scalars().all()
            assert remaining_ub == []
            remaining_db = (await s.execute(
                select(DepartmentInstanceBinding).where(
                    DepartmentInstanceBinding.instance_id == instance_id
                )
            )).scalars().all()
            assert remaining_db == []

            refreshed_owner = await s.get(User, owner_id)
            assert refreshed_owner is not None
            assert refreshed_owner.default_instance_id is None
