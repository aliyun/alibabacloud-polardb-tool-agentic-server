import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.access_control import (
    resolve_agent_instance_access,
    resolve_user_instance_access,
    validate_capability_set,
)
from server.core.binding_manager import get_accessible_instances
from server.mcp.tools import resolve_target_instance
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AllocationMode,
    AuthProvider,
    Base,
    BindingCapability,
    BindingOrigin,
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
    UserStatus,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def test_sql_capabilities_do_not_require_instance_management_capabilities():
    validated = validate_capability_set({BindingCapability.SQL_READ})

    assert validated == frozenset({BindingCapability.SQL_READ})


def test_credentials_read_expands_to_describe_and_list():
    validated = validate_capability_set(
        {BindingCapability.DB_INSTANCE_CREDENTIALS_READ}
    )

    assert validated == frozenset(
        {
            BindingCapability.DB_INSTANCE_CREDENTIALS_READ,
            BindingCapability.DB_INSTANCE_DESCRIBE,
            BindingCapability.DB_INSTANCE_LIST,
        }
    )


def test_sql_write_expands_only_to_sql_read():
    validated = validate_capability_set({BindingCapability.SQL_WRITE})

    assert validated == frozenset(
        {BindingCapability.SQL_READ, BindingCapability.SQL_WRITE}
    )


def test_capability_validation_rejects_non_enum_values():
    with pytest.raises(ValueError, match="Unknown binding capability"):
        validate_capability_set({"sql:read"})  # type: ignore[arg-type]


def test_capability_validation_deduplicates_enum_values():
    validated = validate_capability_set(
        [BindingCapability.SQL_READ, BindingCapability.SQL_READ]
    )

    assert validated == frozenset({BindingCapability.SQL_READ})


async def _user_instance(session):
    user = User(
        external_id="access-control-user",
        display_name="Access Control User",
        auth_provider=AuthProvider.BUILTIN,
    )
    instance = Instance(
        cluster_id="pc-access-control",
        name="Access Control Instance",
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.REGISTERED,
        status=InstanceStatus.ACTIVE,
    )
    session.add_all([user, instance])
    await session.flush()
    return user, instance


async def test_system_binding_preserves_run_sql_without_exposing_credentials(
    session,
):
    user, instance = await _user_instance(session)
    instance.owner_user_id = user.id
    binding = UserInstanceBinding(
        user_id=user.id,
        instance_id=instance.id,
        permission=Permission.READWRITE,
        origin=BindingOrigin.SYSTEM,
    )
    session.add(binding)
    await session.commit()

    access = await resolve_user_instance_access(
        session, user.id, instance.id
    )

    assert access is not None
    assert access.permission == Permission.READWRITE
    assert BindingCapability.SQL_READ in access.capabilities
    assert BindingCapability.SQL_WRITE in access.capabilities
    assert BindingCapability.DB_INSTANCE_LIST not in access.capabilities
    assert (
        BindingCapability.DB_INSTANCE_CREDENTIALS_READ
        not in access.capabilities
    )


async def test_owned_system_binding_ignores_persisted_management_capabilities(
    session,
):
    user, instance = await _user_instance(session)
    instance.owner_user_id = user.id
    binding = UserInstanceBinding(
        user_id=user.id,
        instance_id=instance.id,
        permission=Permission.READWRITE,
        origin=BindingOrigin.SYSTEM,
    )
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_CREDENTIALS_READ
        )
    ]
    session.add(binding)
    await session.commit()

    access = await resolve_user_instance_access(
        session, user.id, instance.id
    )

    assert access is not None
    assert access.capabilities == frozenset(
        {BindingCapability.SQL_READ, BindingCapability.SQL_WRITE}
    )


async def test_department_system_binding_ignores_persisted_management_capabilities(
    session,
):
    user, instance = await _user_instance(session)
    department = Department(name="System Capability Department")
    session.add(department)
    await session.flush()
    session.add_all(
        [
            UserDepartment(
                user_id=user.id, department_id=department.id
            ),
            DepartmentInstanceBinding(
                department_id=department.id,
                instance_id=instance.id,
                default_permission=Permission.READONLY,
            ),
        ]
    )
    binding = UserInstanceBinding(
        user_id=user.id,
        instance_id=instance.id,
        permission=Permission.READWRITE,
        origin=BindingOrigin.SYSTEM,
    )
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_CREDENTIALS_READ
        )
    ]
    session.add(binding)
    await session.commit()

    access = await resolve_user_instance_access(
        session, user.id, instance.id
    )

    assert access is not None
    assert access.access_type == "department"
    assert access.permission == Permission.READONLY
    assert access.capabilities == frozenset(
        {BindingCapability.SQL_READ}
    )


async def test_admin_binding_uses_only_explicit_stored_capabilities(session):
    user, instance = await _user_instance(session)
    binding = UserInstanceBinding(
        user_id=user.id,
        instance_id=instance.id,
        permission=Permission.READWRITE,
        origin=BindingOrigin.ADMIN,
    )
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_DESCRIBE
        )
    ]
    session.add(binding)
    await session.commit()

    access = await resolve_user_instance_access(
        session, user.id, instance.id
    )

    assert access is not None
    assert access.capabilities == frozenset(
        {
            BindingCapability.DB_INSTANCE_LIST,
            BindingCapability.DB_INSTANCE_DESCRIBE,
        }
    )
    assert access.permission is None


async def test_user_permission_intersects_binding_and_credential_capability(
    session,
):
    user, instance = await _user_instance(session)
    credential = InstanceCredential(
        instance_id=instance.id,
        name="readonly-user",
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=CredentialCapability.READONLY,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
    )
    session.add(credential)
    await session.flush()
    binding = UserInstanceBinding(
        user_id=user.id,
        instance_id=instance.id,
        credential_id=credential.id,
        permission=Permission.READWRITE,
        origin=BindingOrigin.ADMIN,
    )
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.SQL_WRITE
        )
    ]
    session.add(binding)
    await session.commit()

    access = await resolve_user_instance_access(
        session, user.id, instance.id
    )

    assert access is not None
    assert access.permission == Permission.READONLY
    assert access.capabilities == frozenset(
        {BindingCapability.SQL_READ, BindingCapability.SQL_WRITE}
    )


@pytest.mark.parametrize("invalid_credential", ["missing", "wrong_owner", "revoked"])
async def test_admin_sql_binding_requires_valid_direct_credential(
    session,
    invalid_credential,
):
    user, instance = await _user_instance(session)
    credential = None
    if invalid_credential != "missing":
        owner = instance
        if invalid_credential == "wrong_owner":
            owner = Instance(
                cluster_id="pc-wrong-access-owner",
                name="Wrong Access Owner",
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.REGISTERED,
                status=InstanceStatus.ACTIVE,
            )
            session.add(owner)
            await session.flush()
        credential = InstanceCredential(
            instance_id=owner.id,
            name=f"{invalid_credential}-credential",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            status=(
                CredentialStatus.REVOKED
                if invalid_credential == "revoked"
                else CredentialStatus.ACTIVE
            ),
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
        )
        session.add(credential)
        await session.flush()

    binding = UserInstanceBinding(
        user_id=user.id,
        instance_id=instance.id,
        credential_id=credential.id if credential is not None else None,
        permission=Permission.READWRITE,
        origin=BindingOrigin.ADMIN,
    )
    binding.capabilities = [
        UserInstanceBindingCapability(
            capability=BindingCapability.SQL_WRITE
        )
    ]
    session.add(binding)
    await session.commit()

    access = await resolve_user_instance_access(
        session, user.id, instance.id
    )

    assert access is not None
    assert access.permission is None
    assert await get_accessible_instances(session, user) == []
    target = await resolve_target_instance(
        user, session, instance_id=instance.id
    )
    assert isinstance(target, dict)
    payload = json.loads(target["content"][0]["text"])
    assert payload["error"] == "INSTANCE_NOT_ACCESSIBLE"


@pytest.mark.parametrize("inherited", [False, True])
async def test_disabled_user_has_no_direct_or_inherited_access(
    session,
    inherited,
):
    user, instance = await _user_instance(session)
    user.status = UserStatus.DISABLED
    if inherited:
        department = Department(name="Disabled User Department")
        session.add(department)
        await session.flush()
        session.add_all(
            [
                UserDepartment(
                    user_id=user.id, department_id=department.id
                ),
                DepartmentInstanceBinding(
                    department_id=department.id,
                    instance_id=instance.id,
                    default_permission=Permission.READONLY,
                ),
            ]
        )
    else:
        instance.owner_user_id = user.id
        session.add(
            UserInstanceBinding(
                user_id=user.id,
                instance_id=instance.id,
                permission=Permission.READONLY,
                origin=BindingOrigin.SYSTEM,
            )
        )
    await session.commit()

    assert (
        await resolve_user_instance_access(
            session, user.id, instance.id
        )
        is None
    )
    assert await get_accessible_instances(session, user) == []


async def test_agent_binding_uses_explicit_capabilities_and_credential_limit(
    session,
):
    creator, instance = await _user_instance(session)
    agent = Agent(name="production-agent", created_by=creator.id)
    credential = InstanceCredential(
        instance_id=instance.id,
        name="agent-readonly",
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=CredentialCapability.READONLY,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
    )
    session.add_all([agent, credential])
    await session.flush()
    binding = AgentInstanceBinding(
        agent_id=agent.id,
        instance_id=instance.id,
        credential_id=credential.id,
        permission=Permission.READWRITE,
        created_by_user_id=creator.id,
    )
    binding.capabilities = [
        AgentInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_CREDENTIALS_READ
        ),
        AgentInstanceBindingCapability(
            capability=BindingCapability.SQL_WRITE
        ),
    ]
    session.add(binding)
    await session.commit()

    access = await resolve_agent_instance_access(
        session, agent.id, instance.id
    )

    assert access is not None
    assert access.permission == Permission.READONLY
    assert access.capabilities == frozenset(BindingCapability)


async def test_disabled_agent_binding_has_no_access(session):
    creator, instance = await _user_instance(session)
    agent = Agent(name="disabled-binding-agent", created_by=creator.id)
    credential = InstanceCredential(
        instance_id=instance.id,
        name="disabled-agent-user",
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=CredentialCapability.READWRITE,
        username_ciphertext="encrypted-user",
        password_ciphertext="encrypted-password",
    )
    session.add_all([agent, credential])
    await session.flush()
    binding = AgentInstanceBinding(
        agent_id=agent.id,
        instance_id=instance.id,
        credential_id=credential.id,
        permission=Permission.READWRITE,
        created_by_user_id=creator.id,
        enabled=False,
    )
    binding.capabilities = [
        AgentInstanceBindingCapability(
            capability=BindingCapability.DB_INSTANCE_LIST
        )
    ]
    session.add(binding)
    await session.commit()

    assert (
        await resolve_agent_instance_access(
            session, agent.id, instance.id
        )
        is None
    )
