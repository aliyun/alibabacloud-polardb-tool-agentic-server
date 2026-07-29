import base64
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import (
    MockPolarDBClient, OpenAPIError, OPENAPI_DUPLICATE_CODES,
    reset_polardb_client, set_polardb_client,
)
from server.config import reset_config
from server.core.crypto import decrypt, encrypt
from server.core.provisioner import (
    ProvisioningError,
    complete_provisioning,
    generate_db_password,
    resolve_primary_endpoint,
)
from server.models import (
    AllocationMode,
    AuthProvider,
    Base,
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    User,
    UserInstanceBinding,
)
from server.models.binding import UserInstanceBindingCapability
from server.models.instance import ProvisioningStep
from server.models.quota_counter import QuotaCounter


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    reset_polardb_client()
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
def mock_client():
    return MockPolarDBClient()


class TestMockPolarDBClient:
    async def test_create_agentic_db(self, mock_client):
        result = await mock_client.create_agentic_db({"region_id": "cn-hangzhou"})
        assert "cluster_id" in result
        assert result["cluster_id"].startswith("pc-mock-")

    async def test_describe_cluster_attribute_default_creating(self, mock_client):
        result = await mock_client.create_agentic_db({"region_id": "cn-hangzhou"})
        attr = await mock_client.describe_cluster_attribute(result["cluster_id"])
        assert attr["status"] == "Creating"

    async def test_advance_to_running(self, mock_client):
        result = await mock_client.create_agentic_db({"region_id": "cn-hangzhou"})
        mock_client.advance_to_running(result["cluster_id"])
        attr = await mock_client.describe_cluster_attribute(result["cluster_id"])
        assert attr["status"] == "Running"

    async def test_create_account(self, mock_client):
        result = await mock_client.create_account("pc-001", "agentic", "pass123")
        assert result["account_name"] == "agentic"

    async def test_create_database(self, mock_client):
        await mock_client.create_database("pc-001", "agentic", "agentic")

    async def test_describe_endpoints(self, mock_client):
        mock_client.set_endpoint_data("pc-001", "pc-001.polardb.rds.aliyuncs.com", 3306)
        result = await mock_client.describe_endpoints("pc-001")
        items = result["items"]
        assert len(items) > 0
        assert items[0]["endpoint_type"] == "Primary"

    async def test_set_create_failure(self, mock_client):
        mock_client.set_create_failure(True)
        with pytest.raises(OpenAPIError):
            await mock_client.create_agentic_db({"region_id": "cn-hangzhou"})

    async def test_set_duplicate_error_account(self, mock_client):
        mock_client.set_duplicate_error("create_account", True)
        with pytest.raises(OpenAPIError) as exc_info:
            await mock_client.create_account("pc-001", "agentic", "pass")
        assert exc_info.value.code in OPENAPI_DUPLICATE_CODES


class TestOpenAPIError:
    def test_error_code(self):
        err = OpenAPIError("InvalidAccountName.Duplicate", "Account exists")
        assert err.code == "InvalidAccountName.Duplicate"
        assert "Account exists" in str(err)

    def test_duplicate_codes_frozenset(self):
        assert "InvalidAccountName.Duplicate" in OPENAPI_DUPLICATE_CODES
        assert "InvalidDBName.Duplicate" in OPENAPI_DUPLICATE_CODES


class TestGenerateDBPassword:
    def test_length_and_format(self):
        pw = generate_db_password()
        assert len(pw) == 19
        assert pw[:3] == "Aa1"
        assert pw[3:].isalnum()

    def test_unique(self):
        passwords = {generate_db_password() for _ in range(100)}
        assert len(passwords) == 100


class TestResolvePrimaryEndpoint:
    async def test_prefers_primary(self, mock_client):
        mock_client.set_endpoint_data("pc-001", "primary.example.com", 3306)
        host, port = await resolve_primary_endpoint(mock_client, "pc-001")
        assert host == "primary.example.com"
        assert port == 3306

    async def test_raises_on_no_endpoint(self, mock_client):
        mock_client._endpoints["pc-001"] = {"items": []}
        with pytest.raises(ProvisioningError, match="no usable endpoint"):
            await resolve_primary_endpoint(mock_client, "pc-001")

    async def test_does_not_fall_back_to_wrong_network_type(
        self, mock_client
    ):
        mock_client._endpoints["pc-001"] = {
            "items": [
                {
                    "endpoint_type": "Primary",
                    "address_items": [
                        {
                            "connection_string": "public.example.com",
                            "port": "3306",
                            "net_type": "Public",
                        }
                    ],
                }
            ]
        }

        with pytest.raises(
            ProvisioningError,
            match="no Private endpoint",
        ):
            await resolve_primary_endpoint(
                mock_client,
                "pc-001",
                preferred_net_type="Private",
            )


# ---------------------------------------------------------------------------
# complete_provisioning tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def user_and_instance(session):
    user = User(
        external_id="prov-test", display_name="Prov User",
        auth_provider=AuthProvider.BUILTIN,
    )
    session.add(user)
    await session.commit()

    counter = QuotaCounter(scope="global", current_count=1, max_limit=50)
    session.add(counter)
    await session.commit()

    inst = Instance(
        cluster_id="pc-mock-test1234", name="auto-provtest",
        engine=InstanceEngine.POLARDB_MYSQL,
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.AUTO_PROVISIONED,
        status=InstanceStatus.CREATING,
        owner_user_id=user.id, provisioning_step=ProvisioningStep.CLUSTER_READY,
        quota_held=True,
    )
    session.add(inst)
    await session.commit()
    return user, inst


class TestCompleteProvisioning:
    @pytest.fixture(autouse=True)
    def _set_encryption_key(self):
        key = base64.b64encode(os.urandom(32)).decode()
        os.environ["PAS_ENCRYPTION_KEY"] = key
        yield
        os.environ.pop("PAS_ENCRYPTION_KEY", None)

    async def test_happy_path_from_cluster_ready(
        self, session_factory, user_and_instance, session,
    ):
        user, inst = user_and_instance
        mock = MockPolarDBClient()
        mock.advance_to_running(inst.cluster_id)
        mock.set_endpoint_data(inst.cluster_id, "test.polardb.com", 3306)
        set_polardb_client(mock)

        await complete_provisioning(inst.id, user.id, session_factory)

        async with session_factory() as s:
            result = await s.get(Instance, inst.id)
            assert result.status == InstanceStatus.ACTIVE
            assert result.provisioning_step == ProvisioningStep.DONE
            assert result.host == "test.polardb.com"
            assert result.port == 3306
            assert result.quota_held is True

            credential = (await s.execute(
                select(InstanceCredential).where(
                    InstanceCredential.instance_id == inst.id
                )
            )).scalar_one()
            assert decrypt(credential.username_ciphertext) == "agentic"
            assert credential.purpose == CredentialPurpose.DIRECT_ACCESS
            assert credential.capability == CredentialCapability.READWRITE

            # Check binding was created
            binding = (await s.execute(
                select(UserInstanceBinding).where(
                    UserInstanceBinding.instance_id == inst.id,
                    UserInstanceBinding.user_id == user.id,
                )
            )).scalar_one()
            assert binding.credential_id == credential.id
            assert binding.origin == BindingOrigin.SYSTEM
            capabilities = set(
                (
                    await s.execute(
                        select(
                            UserInstanceBindingCapability.capability
                        ).where(
                            UserInstanceBindingCapability.binding_id
                            == binding.id
                        )
                    )
                ).scalars()
            )
            assert capabilities == {
                BindingCapability.SQL_READ,
                BindingCapability.SQL_WRITE,
            }

    async def test_failure_marks_failed_and_decrements_quota(
        self, session_factory, user_and_instance, session,
    ):
        user, inst = user_and_instance
        mock = MockPolarDBClient()
        mock.set_create_failure(True)
        set_polardb_client(mock)

        # Start from PENDING (will try to create cluster and fail)
        async with session_factory() as s:
            i = await s.get(Instance, inst.id)
            i.provisioning_step = ProvisioningStep.PENDING
            i.cluster_id = "pending"
            await s.commit()

        await complete_provisioning(inst.id, user.id, session_factory)

        async with session_factory() as s:
            result = await s.get(Instance, inst.id)
            assert result.status == InstanceStatus.FAILED
            assert result.quota_held is False

            counter = (await s.execute(
                select(QuotaCounter).where(QuotaCounter.scope == "global")
            )).scalar_one()
            assert counter.current_count == 0

    async def test_resume_from_password_stored(
        self, session_factory, user_and_instance, session,
    ):
        user, inst = user_and_instance
        mock = MockPolarDBClient()
        mock.advance_to_running(inst.cluster_id)
        mock.set_endpoint_data(inst.cluster_id, "resume.polardb.com", 3306)
        set_polardb_client(mock)

        # Pre-create credential (simulates PASSWORD_STORED before crash).
        async with session_factory() as s:
            i = await s.get(Instance, inst.id)
            i.provisioning_step = ProvisioningStep.PASSWORD_STORED
            pw = generate_db_password()
            credential = InstanceCredential(
                instance_id=inst.id,
                name="agentic",
                purpose=CredentialPurpose.DIRECT_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext=encrypt("agentic"),
                password_ciphertext=encrypt(pw),
                database_name="agentic",
                created_by_user_id=user.id,
            )
            s.add(credential)
            await s.commit()

        await complete_provisioning(inst.id, user.id, session_factory)

        async with session_factory() as s:
            result = await s.get(Instance, inst.id)
            assert result.status == InstanceStatus.ACTIVE
            assert result.provisioning_step == ProvisioningStep.DONE

    async def test_duplicate_account_treated_as_success(
        self, session_factory, user_and_instance, session,
    ):
        user, inst = user_and_instance
        mock = MockPolarDBClient()
        mock.advance_to_running(inst.cluster_id)
        mock.set_endpoint_data(inst.cluster_id, "dup.polardb.com", 3306)
        mock.set_duplicate_error("create_account", True)
        set_polardb_client(mock)

        await complete_provisioning(inst.id, user.id, session_factory)

        async with session_factory() as s:
            result = await s.get(Instance, inst.id)
            assert result.status == InstanceStatus.ACTIVE
