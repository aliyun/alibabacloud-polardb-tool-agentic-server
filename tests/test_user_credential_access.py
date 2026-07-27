import base64
import json
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import (
    MockPolarDBClient,
    reset_polardb_client,
    set_polardb_client,
)
from server.core.binding_manager import (
    create_db_account,
    get_accessible_instances,
    get_user_credential,
)
from server.core.crypto import encrypt
from server.mcp.tools.branch_handler import handle_create_branch
from server.mcp.tools.handlers import (
    handle_run_sql,
    handle_run_sql_transaction,
)
from server.mcp.tools.schema_handler import handle_describe_schema
from server.models import (
    AllocationMode,
    AuthProvider,
    Base,
    BindingCapability,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Department,
    DepartmentInstanceBinding,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    Permission,
    User,
    UserDepartment,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)


@pytest.fixture(autouse=True)
def polardb_client():
    set_polardb_client(MockPolarDBClient())
    os.environ["PAS_ENCRYPTION_KEY"] = base64.b64encode(os.urandom(32)).decode()
    yield
    os.environ.pop("PAS_ENCRYPTION_KEY", None)
    reset_polardb_client()


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def _user_and_instance(session, *, topology=InstanceTopology.SINGLE_TENANT):
    user = User(
        external_id="credential-user",
        display_name="Credential User",
        auth_provider=AuthProvider.BUILTIN,
    )
    instance = Instance(
        cluster_id=f"pc-credential-{topology.value}",
        name="Credential Instance",
        topology=topology,
        allocation_mode=AllocationMode.REGISTERED,
        status=InstanceStatus.ACTIVE,
    )
    session.add_all([user, instance])
    await session.commit()
    return user, instance


def _credential(
    instance,
    user,
    *,
    owner_instance_id=None,
    purpose=CredentialPurpose.DIRECT_ACCESS,
    capability=CredentialCapability.READWRITE,
    status=CredentialStatus.ACTIVE,
):
    return InstanceCredential(
        instance_id=owner_instance_id or instance.id,
        name=f"credential-{os.urandom(4).hex()}",
        purpose=purpose,
        capability=capability,
        status=status,
        username_ciphertext=encrypt("agentic"),
        password_ciphertext=encrypt("password"),
        created_by_user_id=user.id,
    )


def _readwrite_binding(**kwargs):
    binding = UserInstanceBinding(**kwargs)
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.SQL_READ
        ),
        UserInstanceBindingCapability(
            capability=BindingCapability.SQL_WRITE
        ),
    ]
    return binding


async def test_disabled_personal_binding_denies_direct_and_department_access(session):
    user, instance = await _user_and_instance(session)
    department = Department(name="Credential Department")
    session.add(department)
    await session.flush()
    session.add_all(
        [
            UserDepartment(user_id=user.id, department_id=department.id),
            DepartmentInstanceBinding(
                department_id=department.id,
                instance_id=instance.id,
                default_permission=Permission.READWRITE,
            ),
            UserInstanceBinding(
                user_id=user.id,
                instance_id=instance.id,
                permission=Permission.READWRITE,
                enabled=False,
            ),
        ]
    )
    await session.commit()

    assert await get_accessible_instances(session, user) == []
    with pytest.raises(PermissionError):
        await create_db_account(session, instance, user)

    binding = (
        await session.execute(
            UserInstanceBinding.__table__.select().where(
                UserInstanceBinding.user_id == user.id
            )
        )
    ).one()
    assert binding.enabled is False


async def test_disabled_binding_denies_all_user_sql_consumers(session):
    user, instance = await _user_and_instance(session)
    department = Department(name="Disabled Consumer Department")
    session.add(department)
    await session.flush()
    session.add_all(
        [
            UserDepartment(user_id=user.id, department_id=department.id),
            DepartmentInstanceBinding(
                department_id=department.id,
                instance_id=instance.id,
                default_permission=Permission.READWRITE,
            ),
            UserInstanceBinding(
                user_id=user.id,
                instance_id=instance.id,
                permission=Permission.READWRITE,
                enabled=False,
            ),
        ]
    )
    await session.commit()

    results = [
        await handle_run_sql(
            user, session, sql="SELECT 1", instance_id=instance.id
        ),
        await handle_run_sql_transaction(
            user,
            session,
            sql_statements=["SELECT 1"],
            instance_id=instance.id,
        ),
        await handle_create_branch(
            user,
            session,
            branch_name="blocked_branch",
            instance_id=instance.id,
        ),
        await handle_describe_schema(
            user, session, instance_id=instance.id
        ),
    ]

    for result in results:
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "INSTANCE_NOT_ACCESSIBLE"


async def test_department_readonly_materialization_stays_readonly_on_repeated_resolution(
    session,
):
    user, instance = await _user_and_instance(session)
    department = Department(name="Readonly Department")
    session.add(department)
    await session.flush()
    session.add_all(
        [
            UserDepartment(user_id=user.id, department_id=department.id),
            DepartmentInstanceBinding(
                department_id=department.id,
                instance_id=instance.id,
                default_permission=Permission.READONLY,
            ),
        ]
    )
    await session.commit()

    await create_db_account(session, instance, user)
    await session.commit()

    first = await get_user_credential(session, instance.id, user.id)
    second = await get_user_credential(session, instance.id, user.id)
    assert first is not None
    assert second is not None
    assert first.permission == Permission.READONLY
    assert second.permission == Permission.READONLY
    assert first.binding.permission == Permission.READONLY
    assert {item["permission"] for item in await get_accessible_instances(session, user)} == {
        Permission.READONLY.value
    }


async def test_department_materialized_binding_does_not_survive_department_unbind(
    session,
):
    user, instance = await _user_and_instance(session)
    department = Department(name="Revoked Department")
    session.add(department)
    await session.flush()
    membership = UserDepartment(
        user_id=user.id, department_id=department.id
    )
    department_binding = DepartmentInstanceBinding(
        department_id=department.id,
        instance_id=instance.id,
        default_permission=Permission.READONLY,
    )
    session.add_all([membership, department_binding])
    await session.commit()

    await create_db_account(session, instance, user)
    await session.commit()
    await session.delete(department_binding)
    await session.commit()

    assert await get_user_credential(session, instance.id, user.id) is None
    assert await get_accessible_instances(session, user) == []


@pytest.mark.parametrize(
    ("mutation", "expected_permission"),
    [
        ("wrong_owner", None),
        ("provisioning_admin", None),
        ("resource_access", None),
        ("revoked", None),
        ("admin", None),
        ("readonly", Permission.READONLY),
    ],
)
async def test_user_credential_resolution_validates_contract_and_intersects_permission(
    session,
    mutation,
    expected_permission,
):
    user, instance = await _user_and_instance(session)
    other = Instance(
        cluster_id="pc-other-owner",
        name="Other Owner",
        allocation_mode=AllocationMode.REGISTERED,
    )
    session.add(other)
    await session.flush()

    kwargs = {}
    if mutation == "wrong_owner":
        kwargs["owner_instance_id"] = other.id
    elif mutation == "provisioning_admin":
        kwargs["purpose"] = CredentialPurpose.PROVISIONING_ADMIN
        kwargs["capability"] = CredentialCapability.ADMIN
    elif mutation == "resource_access":
        kwargs["purpose"] = CredentialPurpose.RESOURCE_ACCESS
    elif mutation == "revoked":
        kwargs["status"] = CredentialStatus.REVOKED
    elif mutation == "admin":
        kwargs["capability"] = CredentialCapability.ADMIN
    elif mutation == "readonly":
        kwargs["capability"] = CredentialCapability.READONLY

    credential = _credential(instance, user, **kwargs)
    session.add(credential)
    await session.flush()
    session.add(
        _readwrite_binding(
            user_id=user.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
        )
    )
    await session.commit()

    resolved = await get_user_credential(session, instance.id, user.id)
    if expected_permission is None:
        assert resolved is None
    else:
        assert resolved is not None
        assert resolved.permission == expected_permission


async def test_provisioning_admin_credential_is_rejected_by_all_user_sql_consumers(
    session,
):
    user, instance = await _user_and_instance(session)
    credential = _credential(
        instance,
        user,
        purpose=CredentialPurpose.PROVISIONING_ADMIN,
        capability=CredentialCapability.ADMIN,
    )
    session.add(credential)
    await session.flush()
    session.add(
        _readwrite_binding(
            user_id=user.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
        )
    )
    await session.commit()

    results = [
        await handle_run_sql(
            user, session, sql="SELECT 1", instance_id=instance.id
        ),
        await handle_run_sql_transaction(
            user,
            session,
            sql_statements=["SELECT 1"],
            instance_id=instance.id,
        ),
        await handle_create_branch(
            user,
            session,
            branch_name="invalid_credential",
            instance_id=instance.id,
        ),
        await handle_describe_schema(
            user, session, instance_id=instance.id
        ),
    ]

    for result in results:
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "INSTANCE_NOT_ACCESSIBLE"


async def test_readwrite_binding_with_readonly_credential_rejects_repeated_writes(
    session,
):
    user, instance = await _user_and_instance(session)
    credential = _credential(
        instance,
        user,
        capability=CredentialCapability.READONLY,
    )
    session.add(credential)
    await session.flush()
    session.add(
        _readwrite_binding(
            user_id=user.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
        )
    )
    await session.commit()

    for _ in range(2):
        result = await handle_run_sql(
            user,
            session,
            sql="INSERT INTO protected_table VALUES (1)",
            instance_id=instance.id,
        )
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "READ_ONLY_ACCESS"
