"""Tests for the provisioning State Pattern implementation."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import (
    MockPolarDBClient,
    OpenAPIError,
    reset_polardb_client,
    set_polardb_client,
)
from server.config import reset_config
from server.core.crypto import encrypt
from server.core.provisioner import generate_db_password
from server.core.provisioning import (
    AccountCreatedState,
    BoundState,
    ClusterReadyState,
    CompletedState,
    DatabaseCreatedState,
    EndpointResolvedState,
    FailedState,
    PasswordStoredState,
    PendingState,
    ProvisioningContext,
    run_provisioning,
    state_from_step,
)
from server.models import (
    AccountType,
    AuthProvider,
    Base,
    DBAccount,
    Instance,
    InstanceStatus,
    InstanceType,
    User,
    UserInstanceBinding,
)
from server.models.binding import UserDepartment
from server.models.department import Department
from server.models.instance import ProvisioningStep
from server.models.quota_counter import QuotaCounter
from server.models.user import ProvisioningMode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean():
    reset_config()
    reset_polardb_client()
    yield
    reset_config()
    reset_polardb_client()


@pytest.fixture(autouse=True)
def _set_encryption_key():
    key = base64.b64encode(os.urandom(32)).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key
    yield
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
async def seeded(session_factory):
    """Insert a user, quota counter, and a CREATING instance."""
    async with session_factory() as s:
        user = User(
            external_id="states-test",
            display_name="States User",
            auth_provider=AuthProvider.BUILTIN,
        )
        s.add(user)
        await s.commit()

        s.add(QuotaCounter(scope="global", current_count=1, max_limit=50))
        await s.commit()

        inst = Instance(
            cluster_id="pc-mock-states01",
            name="states-inst",
            type=InstanceType.PERSONAL,
            status=InstanceStatus.CREATING,
            owner_user_id=user.id,
            provisioning_step=ProvisioningStep.PENDING,
            quota_held=True,
        )
        s.add(inst)
        await s.commit()
        return user.id, inst.id


def _make_ctx(session, instance, session_factory, client_or_user_id, user_id=None):
    import time
    # Support both _make_ctx(s, inst, sf, client, user_id) and _make_ctx(s, inst, sf, user_id)
    if user_id is None:
        user_id = client_or_user_id
    else:
        from server.aliyun.polardb_client import set_polardb_client
        set_polardb_client(client_or_user_id)
    return ProvisioningContext(
        instance=instance,
        session=session,
        session_factory=session_factory,
        instance_id=instance.id,
        user_id=user_id,
        start_time=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# state_from_step
# ---------------------------------------------------------------------------


class TestStateFromStep:
    def test_maps_each_step(self):
        assert isinstance(state_from_step(ProvisioningStep.PENDING), PendingState)
        assert isinstance(
            state_from_step(ProvisioningStep.CLUSTER_READY), ClusterReadyState
        )
        assert isinstance(
            state_from_step(ProvisioningStep.PASSWORD_STORED), PasswordStoredState
        )
        assert isinstance(
            state_from_step(ProvisioningStep.ACCOUNT_CREATED), AccountCreatedState
        )
        assert isinstance(
            state_from_step(ProvisioningStep.DATABASE_CREATED),
            DatabaseCreatedState,
        )
        assert isinstance(
            state_from_step(ProvisioningStep.ENDPOINT_RESOLVED),
            EndpointResolvedState,
        )
        assert isinstance(state_from_step(ProvisioningStep.BOUND), BoundState)
        assert isinstance(state_from_step(ProvisioningStep.DONE), CompletedState)


# ---------------------------------------------------------------------------
# Individual state transitions (single-step)
# ---------------------------------------------------------------------------


class TestPendingState:
    async def test_creates_cluster_when_pending_id(
        self, session_factory, seeded
    ):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.cluster_id = "pending-xyz"
            await s.commit()

            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            state = PendingState()
            # Pre-advance the cluster on first observation
            original_create = mock.create_agentic_db

            async def create_and_advance(settings):
                result = await original_create(settings)
                mock.advance_to_running(result["cluster_id"])
                return result

            mock.create_agentic_db = create_and_advance  # type: ignore[assignment]
            next_state = await state.execute(ctx)

        assert isinstance(next_state, ClusterReadyState)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.cluster_id.startswith("pc-mock-")
            assert inst.provisioning_step == ProvisioningStep.CLUSTER_READY

    async def test_skips_create_when_cluster_id_set(
        self, session_factory, seeded
    ):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            mock.advance_to_running(inst.cluster_id)
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await PendingState().execute(ctx)

        assert isinstance(next_state, ClusterReadyState)

    async def test_failure_returns_failed_state(self, session_factory, seeded):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        mock.set_create_failure(True)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.cluster_id = "pending-fail"
            await s.commit()
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await PendingState().execute(ctx)

        assert isinstance(next_state, FailedState)
        assert next_state.error is not None

        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.status == InstanceStatus.FAILED
            assert inst.quota_held is False


class TestClusterReadyState:
    async def test_stores_password(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.CLUSTER_READY
            await s.commit()
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            next_state = await ClusterReadyState().execute(ctx)

        assert isinstance(next_state, PasswordStoredState)
        async with session_factory() as s:
            acc = (
                await s.execute(
                    select(DBAccount).where(DBAccount.instance_id == inst_id)
                )
            ).scalar_one()
            assert acc.account_name == "agentic"
            assert acc.account_password_enc


class TestPasswordStoredState:
    async def test_creates_account(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.PASSWORD_STORED
            s.add(
                DBAccount(
                    instance_id=inst_id,
                    user_id=user_id,
                    account_name="agentic",
                    account_password_enc=encrypt(generate_db_password()),
                    account_type=AccountType.NORMAL,
                )
            )
            await s.commit()
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            next_state = await PasswordStoredState().execute(ctx)

        assert isinstance(next_state, AccountCreatedState)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.provisioning_step == ProvisioningStep.ACCOUNT_CREATED

    async def test_duplicate_account_treated_as_success(
        self, session_factory, seeded
    ):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        mock.set_duplicate_error("create_account", True)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.PASSWORD_STORED
            s.add(
                DBAccount(
                    instance_id=inst_id,
                    user_id=user_id,
                    account_name="agentic",
                    account_password_enc=encrypt(generate_db_password()),
                    account_type=AccountType.NORMAL,
                )
            )
            await s.commit()
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await PasswordStoredState().execute(ctx)

        assert isinstance(next_state, AccountCreatedState)

    async def test_non_duplicate_openapi_error_fails(
        self, session_factory, seeded
    ):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        mock.create_account = AsyncMock(  # type: ignore[assignment]
            side_effect=OpenAPIError("Some.Other.Error", "boom")
        )
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.PASSWORD_STORED
            s.add(
                DBAccount(
                    instance_id=inst_id,
                    user_id=user_id,
                    account_name="agentic",
                    account_password_enc=encrypt(generate_db_password()),
                    account_type=AccountType.NORMAL,
                )
            )
            await s.commit()
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await PasswordStoredState().execute(ctx)

        assert isinstance(next_state, FailedState)


class TestAccountCreatedState:
    async def test_creates_database(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.ACCOUNT_CREATED
            await s.commit()
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            next_state = await AccountCreatedState().execute(ctx)

        assert isinstance(next_state, DatabaseCreatedState)

    async def test_duplicate_db_treated_as_success(
        self, session_factory, seeded
    ):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        mock.set_duplicate_error("create_database", True)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.ACCOUNT_CREATED
            await s.commit()
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await AccountCreatedState().execute(ctx)

        assert isinstance(next_state, DatabaseCreatedState)


class TestDatabaseCreatedState:
    async def test_resolves_endpoint(self, session_factory, seeded):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.DATABASE_CREATED
            mock.set_endpoint_data(inst.cluster_id, "host.example.com", 3306)
            await s.commit()
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await DatabaseCreatedState().execute(ctx)

        assert isinstance(next_state, EndpointResolvedState)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.host == "host.example.com"
            assert inst.port == 3306

    async def test_missing_endpoint_fails(self, session_factory, seeded):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.DATABASE_CREATED
            mock._endpoints[inst.cluster_id] = {"items": []}
            await s.commit()
            ctx = _make_ctx(s, inst, session_factory, mock, user_id)
            next_state = await DatabaseCreatedState().execute(ctx)

        assert isinstance(next_state, FailedState)


class TestEndpointResolvedState:
    async def test_creates_binding_and_default(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.ENDPOINT_RESOLVED
            await s.commit()
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            next_state = await EndpointResolvedState().execute(ctx)

        assert isinstance(next_state, BoundState)
        async with session_factory() as s:
            user = await s.get(User, user_id)
            assert user.default_instance_id == inst_id
            binding = (
                await s.execute(
                    select(UserInstanceBinding).where(
                        UserInstanceBinding.instance_id == inst_id,
                        UserInstanceBinding.user_id == user_id,
                    )
                )
            ).scalar_one()
            assert binding is not None


class TestBoundState:
    async def test_marks_active_and_done(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.provisioning_step = ProvisioningStep.BOUND
            await s.commit()
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            next_state = await BoundState().execute(ctx)

        assert isinstance(next_state, CompletedState)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.status == InstanceStatus.ACTIVE
            assert inst.provisioning_step == ProvisioningStep.DONE


class TestTerminalStates:
    async def test_completed_returns_self(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            state = CompletedState()
            assert (await state.execute(ctx)) is state

    async def test_failed_returns_self(self, session_factory, seeded):
        user_id, inst_id = seeded
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            ctx = _make_ctx(
                s, inst, session_factory, MockPolarDBClient(), user_id
            )
            state = FailedState(RuntimeError("boom"))
            assert (await state.execute(ctx)) is state


# ---------------------------------------------------------------------------
# run_provisioning end-to-end
# ---------------------------------------------------------------------------


class TestRunProvisioning:
    async def test_full_chain_from_pending(self, session_factory, seeded):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            mock.advance_to_running(inst.cluster_id)
            mock.set_endpoint_data(inst.cluster_id, "e2e.example.com", 3306)

        set_polardb_client(mock)
        result = await run_provisioning(
            inst_id, user_id, session_factory
        )

        assert isinstance(result, CompletedState)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.status == InstanceStatus.ACTIVE
            assert inst.provisioning_step == ProvisioningStep.DONE
            assert inst.host == "e2e.example.com"

    async def test_failure_during_pending_returns_failed(
        self, session_factory, seeded
    ):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        mock.set_create_failure(True)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            inst.cluster_id = "pending"
            await s.commit()

        set_polardb_client(mock)
        result = await run_provisioning(
            inst_id, user_id, session_factory
        )

        assert isinstance(result, FailedState)
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            assert inst.status == InstanceStatus.FAILED
            assert inst.quota_held is False

    async def test_resume_from_password_stored(self, session_factory, seeded):
        user_id, inst_id = seeded
        mock = MockPolarDBClient()
        async with session_factory() as s:
            inst = await s.get(Instance, inst_id)
            mock.advance_to_running(inst.cluster_id)
            mock.set_endpoint_data(inst.cluster_id, "resume.example.com", 3306)
            inst.provisioning_step = ProvisioningStep.PASSWORD_STORED
            s.add(
                DBAccount(
                    instance_id=inst_id,
                    user_id=user_id,
                    account_name="agentic",
                    account_password_enc=encrypt(generate_db_password()),
                    account_type=AccountType.NORMAL,
                )
            )
            await s.commit()

        set_polardb_client(mock)
        result = await run_provisioning(
            inst_id, user_id, session_factory
        )
        assert isinstance(result, CompletedState)

    async def test_missing_instance_returns_failed(self, session_factory):
        set_polardb_client(MockPolarDBClient())
        result = await run_provisioning(
            "nonexistent-id",
            "nonexistent-user",
            session_factory,
        )
        assert isinstance(result, FailedState)


# ---------------------------------------------------------------------------
# Helper for dedicated path tests
# ---------------------------------------------------------------------------


class DedicatedMockClient(MockPolarDBClient):
    """MockPolarDBClient with predictable create_dedicated_cluster results."""

    def __init__(self):
        super().__init__()
        self._dedicated_call_count = 0
        self._last_agentic_cluster_id: str | None = None

    async def create_dedicated_cluster(self, params, agentic_db_type, agentic_db_cluster_id, agentic_db_cluster_description, db_cluster_description):
        self._dedicated_call_count += 1
        self._last_agentic_cluster_id = agentic_db_cluster_id
        cluster_id = f"pc-mock-ded-{self._dedicated_call_count:04d}"
        returned_agentic_id = agentic_db_cluster_id or f"pagc-mock-{self._dedicated_call_count:04d}"
        return {
            "cluster_id": cluster_id,
            "agentic_db_cluster_id": returned_agentic_id,
            "agentic_db_cluster_description": agentic_db_cluster_description,
        }


# ---------------------------------------------------------------------------
# Dedicated path tests
# ---------------------------------------------------------------------------


class TestPendingStateDedicated:
    """Tests for PendingState with dedicated + Department path."""

    async def test_first_dedicated_creates_and_backfills_department(self, engine):
        """First dedicated instance for a department creates new AgenticDbClusterId."""
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        mock_client = DedicatedMockClient()
        set_polardb_client(mock_client)

        async with session_factory() as session:
            user = User(
                external_id="ded-user-1", display_name="Ded User",
                auth_provider=AuthProvider.BUILTIN,
                provisioning_mode=ProvisioningMode.DEDICATED,
            )
            session.add(user)
            await session.flush()

            dept = Department(name="Engineering")
            session.add(dept)
            await session.flush()

            membership = UserDepartment(user_id=user.id, department_id=dept.id, is_primary=True)
            session.add(membership)
            await session.flush()

            instance = Instance(
                cluster_id="pending-ded-1",
                name="Ded Instance",
                type=InstanceType.PERSONAL,
                status=InstanceStatus.CREATING,
                provisioning_step=ProvisioningStep.PENDING,
                owner_user_id=user.id,
            )
            session.add(instance)
            await session.commit()

            # Pre-register the cluster as Running so _poll_until_running passes
            mock_client._clusters["pc-mock-ded-0001"] = {"status": "Running"}

            ctx = ProvisioningContext(
                instance=instance,
                session=session,
                session_factory=session_factory,
                instance_id=instance.id,
                user_id=user.id,
                start_time=0.0,
            )
            state = PendingState()
            next_state = await state.execute(ctx)

            assert isinstance(next_state, ClusterReadyState)
            await session.refresh(dept)
            assert dept.agentic_db_cluster_id is not None
            assert dept.agentic_db_cluster_id == "pagc-mock-0001"

    async def test_second_dedicated_reuses_existing_cluster_id(self, engine):
        """Second dedicated instance passes existing AgenticDbClusterId."""
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        mock_client = DedicatedMockClient()
        set_polardb_client(mock_client)

        async with session_factory() as session:
            user = User(
                external_id="ded-user-2", display_name="Ded User 2",
                auth_provider=AuthProvider.BUILTIN,
                provisioning_mode=ProvisioningMode.DEDICATED,
            )
            session.add(user)
            await session.flush()

            dept = Department(
                name="Engineering2",
                agentic_db_cluster_id="pagc-existing-123",
            )
            session.add(dept)
            await session.flush()

            membership = UserDepartment(user_id=user.id, department_id=dept.id, is_primary=True)
            session.add(membership)
            await session.flush()

            instance = Instance(
                cluster_id="pending-ded-2",
                name="Ded Instance 2",
                type=InstanceType.PERSONAL,
                status=InstanceStatus.CREATING,
                provisioning_step=ProvisioningStep.PENDING,
                owner_user_id=user.id,
            )
            session.add(instance)
            await session.commit()

            # Pre-register the cluster as Running
            mock_client._clusters["pc-mock-ded-0001"] = {"status": "Running"}

            ctx = ProvisioningContext(
                instance=instance,
                session=session,
                session_factory=session_factory,
                instance_id=instance.id,
                user_id=user.id,
                start_time=0.0,
            )
            state = PendingState()
            next_state = await state.execute(ctx)

            assert isinstance(next_state, ClusterReadyState)
            await session.refresh(dept)
            assert dept.agentic_db_cluster_id == "pagc-existing-123"
            # Verify the existing ID was passed to the client
            assert mock_client._last_agentic_cluster_id == "pagc-existing-123"

    async def test_default_dedicated_user_uses_new_path(self, engine):
        """User with provisioning_mode=None defaults to DEDICATED path."""
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        mock_client = DedicatedMockClient()
        set_polardb_client(mock_client)

        async with session_factory() as session:
            user = User(
                external_id="default-ded-user", display_name="Default Ded",
                auth_provider=AuthProvider.BUILTIN,
                provisioning_mode=None,
            )
            session.add(user)
            await session.flush()

            dept = Department(name="DefaultDept")
            session.add(dept)
            await session.flush()

            membership = UserDepartment(user_id=user.id, department_id=dept.id, is_primary=True)
            session.add(membership)
            await session.flush()

            instance = Instance(
                cluster_id="pending-default-1",
                name="Default Ded Instance",
                type=InstanceType.PERSONAL,
                status=InstanceStatus.CREATING,
                provisioning_step=ProvisioningStep.PENDING,
                owner_user_id=user.id,
            )
            session.add(instance)
            await session.commit()

            mock_client._clusters["pc-mock-ded-0001"] = {"status": "Running"}

            ctx = ProvisioningContext(
                instance=instance,
                session=session,
                session_factory=session_factory,
                instance_id=instance.id,
                user_id=user.id,
                start_time=0.0,
            )
            state = PendingState()
            next_state = await state.execute(ctx)

            assert isinstance(next_state, ClusterReadyState)
            await session.refresh(dept)
            assert dept.agentic_db_cluster_id is not None
            assert mock_client._dedicated_call_count == 1

    async def test_multitenant_user_skips_agentic_path(self, engine):
        """MULTITENANT user goes through old create_agentic_db path."""
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        mock_client = MockPolarDBClient()
        set_polardb_client(mock_client)

        async with session_factory() as session:
            user = User(
                external_id="mt-user", display_name="MT User",
                auth_provider=AuthProvider.BUILTIN,
                provisioning_mode=ProvisioningMode.MULTITENANT,
            )
            session.add(user)
            await session.flush()

            dept = Department(name="MTDept")
            session.add(dept)
            await session.flush()

            membership = UserDepartment(user_id=user.id, department_id=dept.id, is_primary=True)
            session.add(membership)
            await session.flush()

            instance = Instance(
                cluster_id="pending-mt-1",
                name="MT Instance",
                type=InstanceType.PERSONAL,
                status=InstanceStatus.CREATING,
                provisioning_step=ProvisioningStep.PENDING,
                owner_user_id=user.id,
            )
            session.add(instance)
            await session.commit()

            # Old path: create_agentic_db registers in _clusters as "Creating"
            # We need to make _poll_until_running succeed
            original_describe = mock_client.describe_cluster_attribute

            async def _always_running(cluster_id):
                return {"status": "Running"}
            mock_client.describe_cluster_attribute = _always_running

            ctx = ProvisioningContext(
                instance=instance,
                session=session,
                session_factory=session_factory,
                instance_id=instance.id,
                user_id=user.id,
                start_time=0.0,
            )
            state = PendingState()
            next_state = await state.execute(ctx)

            mock_client.describe_cluster_attribute = original_describe

            assert isinstance(next_state, ClusterReadyState)
            await session.refresh(dept)
            assert dept.agentic_db_cluster_id is None

    async def test_dedicated_user_without_department_uses_old_path(self, engine):
        """Dedicated user with no department falls back to old path."""
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        mock_client = MockPolarDBClient()
        set_polardb_client(mock_client)

        async with session_factory() as session:
            user = User(
                external_id="ded-no-dept", display_name="No Dept",
                auth_provider=AuthProvider.BUILTIN,
                provisioning_mode=ProvisioningMode.DEDICATED,
            )
            session.add(user)
            await session.flush()

            instance = Instance(
                cluster_id="pending-nodept-1",
                name="NoDept Instance",
                type=InstanceType.PERSONAL,
                status=InstanceStatus.CREATING,
                provisioning_step=ProvisioningStep.PENDING,
                owner_user_id=user.id,
            )
            session.add(instance)
            await session.commit()

            original_describe = mock_client.describe_cluster_attribute

            async def _always_running(cluster_id):
                return {"status": "Running"}
            mock_client.describe_cluster_attribute = _always_running

            ctx = ProvisioningContext(
                instance=instance,
                session=session,
                session_factory=session_factory,
                instance_id=instance.id,
                user_id=user.id,
                start_time=0.0,
            )
            state = PendingState()
            next_state = await state.execute(ctx)

            mock_client.describe_cluster_attribute = original_describe

            assert isinstance(next_state, ClusterReadyState)
            # Should have used old path (cluster_id starts with pc-mock-)
            await session.refresh(instance)
            assert instance.cluster_id.startswith("pc-mock-")
