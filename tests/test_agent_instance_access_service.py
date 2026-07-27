from __future__ import annotations

import pytest
from sqlalchemy import func, select

from server.core.agent_instance_access_service import (
    AgentInstanceAccessCapability,
    BindingHasResources,
    CreateAvailability,
    DirectCredentialRequired,
    InstanceNotBindable,
    InstanceNotMultitenant,
    ProvisioningBackendRequired,
    ProvisioningBackendUnavailable,
    delete_agent_instance_access,
    list_agent_instance_access,
    upsert_agent_instance_access,
)
from server.core.crypto import encrypt
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentProvisioningBinding,
    AllocationMode,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
)
from server.models.base import utc_now

pytest_plugins = ("tests._admin_api_fixtures",)


async def _seed_context(setup):
    factory, admin, _ = setup
    async with factory() as session:
        agent = Agent(name="aggregate-agent", created_by=admin.id)
        direct_instance = Instance(
            cluster_id="aggregate-direct",
            name="aggregate-direct",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="direct.example.invalid",
            port=3306,
        )
        multitenant = Instance(
            cluster_id="aggregate-multitenant",
            name="aggregate-multitenant",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="multitenant.example.invalid",
            port=3306,
        )
        session.add_all([agent, direct_instance, multitenant])
        await session.flush()
        direct_credential = InstanceCredential(
            instance_id=direct_instance.id,
            name="direct-reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("secret"),
            database_name="app",
            created_by_user_id=admin.id,
        )
        multitenant_direct_credential = InstanceCredential(
            instance_id=multitenant.id,
            name="tenant-reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("tenant_reader"),
            password_ciphertext=encrypt("secret"),
            database_name="mysql",
            created_by_user_id=admin.id,
        )
        admin_credential = InstanceCredential(
            instance_id=multitenant.id,
            name="tenant-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("admin"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add_all(
            [
                direct_credential,
                multitenant_direct_credential,
                admin_credential,
            ]
        )
        await session.flush()
        backend = ProvisioningBackend(
            instance_id=multitenant.id,
            admin_credential_id=admin_credential.id,
            status=ProvisioningBackendStatus.ACTIVE,
            priority=0,
            max_active_resources=10,
            resource_min_cpu=0,
            resource_max_cpu=2,
            ddl_concurrency=2,
        )
        session.add(backend)
        await session.flush()
        session.add(
            ProvisioningBackendHealth(
                backend_id=backend.id,
                healthy=True,
                checked_at=utc_now(),
                consecutive_failures=0,
            )
        )
        await session.commit()
        return {
            "admin_id": admin.id,
            "agent_id": agent.id,
            "direct_instance_id": direct_instance.id,
            "direct_credential_id": direct_credential.id,
            "multitenant_id": multitenant.id,
            "multitenant_direct_credential_id":
                multitenant_direct_credential.id,
            "backend_id": backend.id,
        }


@pytest.mark.parametrize(
    "status", [InstanceStatus.CREATING, InstanceStatus.FAILED]
)
async def test_upsert_rejects_unavailable_instance(setup, status):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        pending = Instance(
            cluster_id=f"pool-pending-{status.value}",
            name="pool-pending",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.POOLED,
            status=status,
        )
        session.add(pending)
        await session.commit()
        with pytest.raises(InstanceNotBindable):
            await upsert_agent_instance_access(
                session,
                agent_id=context["agent_id"],
                instance_id=pending.id,
                credential_id=None,
                permission=None,
                direct_enabled=None,
                capabilities={
                    AgentInstanceAccessCapability.DB_INSTANCE_LIST
                },
                admin_id=context["admin_id"],
                require_existing=False,
            )


async def test_upsert_allows_provisioning_only_without_direct_credential(
    setup,
):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        view = await upsert_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["multitenant_id"],
            credential_id=None,
            permission=None,
            direct_enabled=None,
            capabilities={
                AgentInstanceAccessCapability.DB_INSTANCE_CREATE
            },
            admin_id=context["admin_id"],
            require_existing=False,
        )
        await session.commit()

        assert view.direct_binding_id is None
        assert view.provisioning_binding_id is not None
        assert view.capabilities == (
            AgentInstanceAccessCapability.DB_INSTANCE_CREATE,
        )
        assert view.create_availability == CreateAvailability.AVAILABLE
        assert await session.scalar(
            select(func.count()).select_from(AgentInstanceBinding)
        ) == 0


async def test_upsert_combines_direct_and_provisioning_access(setup):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        view = await upsert_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["multitenant_id"],
            credential_id=context["multitenant_direct_credential_id"],
            permission=Permission.READONLY,
            direct_enabled=True,
            capabilities={
                AgentInstanceAccessCapability.DB_INSTANCE_DESCRIBE,
                AgentInstanceAccessCapability.SQL_READ,
                AgentInstanceAccessCapability.DB_INSTANCE_CREATE,
            },
            admin_id=context["admin_id"],
            require_existing=False,
        )
        await session.commit()

        assert view.direct_binding_id is not None
        assert view.provisioning_binding_id is not None
        assert view.capabilities == (
            AgentInstanceAccessCapability.DB_INSTANCE_LIST,
            AgentInstanceAccessCapability.DB_INSTANCE_DESCRIBE,
            AgentInstanceAccessCapability.SQL_READ,
            AgentInstanceAccessCapability.DB_INSTANCE_CREATE,
        )


async def test_upsert_requires_direct_fields_for_direct_capabilities(setup):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        with pytest.raises(
            DirectCredentialRequired,
            match="direct-access credential",
        ):
            await upsert_agent_instance_access(
                session,
                agent_id=context["agent_id"],
                instance_id=context["multitenant_id"],
                credential_id=None,
                permission=None,
                direct_enabled=None,
                capabilities={
                    AgentInstanceAccessCapability.SQL_READ
                },
                admin_id=context["admin_id"],
                require_existing=False,
            )


async def test_create_requires_multitenant_instance_and_healthy_backend(
    setup,
):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        with pytest.raises(InstanceNotMultitenant):
            await upsert_agent_instance_access(
                session,
                agent_id=context["agent_id"],
                instance_id=context["direct_instance_id"],
                credential_id=None,
                permission=None,
                direct_enabled=None,
                capabilities={
                    AgentInstanceAccessCapability.DB_INSTANCE_CREATE
                },
                admin_id=context["admin_id"],
                require_existing=False,
            )

        backend = await session.get(
            ProvisioningBackend, context["backend_id"]
        )
        assert backend is not None
        await session.delete(backend.health)
        await session.commit()

    async with factory() as session:
        with pytest.raises(ProvisioningBackendUnavailable):
            await upsert_agent_instance_access(
                session,
                agent_id=context["agent_id"],
                instance_id=context["multitenant_id"],
                credential_id=None,
                permission=None,
                direct_enabled=None,
                capabilities={
                    AgentInstanceAccessCapability.DB_INSTANCE_CREATE
                },
                admin_id=context["admin_id"],
                require_existing=False,
            )


async def test_create_requires_configured_backend(setup):
    factory, admin, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        without_backend = Instance(
            cluster_id="aggregate-no-backend",
            name="aggregate-no-backend",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(without_backend)
        await session.flush()
        with pytest.raises(ProvisioningBackendRequired):
            await upsert_agent_instance_access(
                session,
                agent_id=context["agent_id"],
                instance_id=without_backend.id,
                credential_id=None,
                permission=None,
                direct_enabled=None,
                capabilities={
                    AgentInstanceAccessCapability.DB_INSTANCE_CREATE
                },
                admin_id=admin.id,
                require_existing=False,
            )


async def test_disabling_create_preserves_resources_and_blocks_delete(setup):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        created = await upsert_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["multitenant_id"],
            credential_id=None,
            permission=None,
            direct_enabled=None,
            capabilities={
                AgentInstanceAccessCapability.DB_INSTANCE_CREATE
            },
            admin_id=context["admin_id"],
            require_existing=False,
        )
        resource = DBInstanceResource(
            owner_agent_id=context["agent_id"],
            backend_id=context["backend_id"],
            client_token="aggregate-resource",
            request_fingerprint="a" * 64,
            engine=InstanceEngine.POLARDB_MYSQL,
            status=DBInstanceStatus.READY,
        )
        session.add(resource)
        await session.flush()

        updated = await upsert_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["multitenant_id"],
            credential_id=None,
            permission=None,
            direct_enabled=None,
            capabilities=set(),
            admin_id=context["admin_id"],
            require_existing=True,
        )
        assert updated.capabilities == ()
        assert (
            updated.provisioning_binding_id
            == created.provisioning_binding_id
        )
        binding = await session.get(
            AgentProvisioningBinding,
            created.provisioning_binding_id,
        )
        assert binding is not None
        assert binding.enabled is False

        with pytest.raises(BindingHasResources):
            await delete_agent_instance_access(
                session,
                agent_id=context["agent_id"],
                instance_id=context["multitenant_id"],
            )

        resource.status = DBInstanceStatus.DELETED
        await session.flush()
        deleted = await delete_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["multitenant_id"],
        )
        assert deleted.instance_id == context["multitenant_id"]
        await session.commit()
        assert await session.get(
            AgentProvisioningBinding,
            created.provisioning_binding_id,
        ) is None


async def test_list_merges_direct_and_provisioning_records_by_instance(
    setup,
):
    factory, _, _ = setup
    context = await _seed_context(setup)
    async with factory() as session:
        await upsert_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["direct_instance_id"],
            credential_id=context["direct_credential_id"],
            permission=Permission.READONLY,
            direct_enabled=True,
            capabilities={
                AgentInstanceAccessCapability.DB_INSTANCE_LIST
            },
            admin_id=context["admin_id"],
            require_existing=False,
        )
        await upsert_agent_instance_access(
            session,
            agent_id=context["agent_id"],
            instance_id=context["multitenant_id"],
            credential_id=None,
            permission=None,
            direct_enabled=None,
            capabilities={
                AgentInstanceAccessCapability.DB_INSTANCE_CREATE
            },
            admin_id=context["admin_id"],
            require_existing=False,
        )
        await session.commit()

    async with factory() as session:
        rows = await list_agent_instance_access(
            session, context["agent_id"]
        )
        by_instance = {row.instance_id: row for row in rows}
        assert set(by_instance) == {
            context["direct_instance_id"],
            context["multitenant_id"],
        }
        assert by_instance[
            context["direct_instance_id"]
        ].capabilities == (
            AgentInstanceAccessCapability.DB_INSTANCE_LIST,
        )
        assert by_instance[
            context["multitenant_id"]
        ].capabilities == (
            AgentInstanceAccessCapability.DB_INSTANCE_CREATE,
        )
