from __future__ import annotations

from sqlalchemy import select

from server.core.crypto import encrypt
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentProvisioningBinding,
    AllocationMode,
    AuditLog,
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
)
from server.models.base import utc_now

pytest_plugins = ("tests._admin_api_fixtures",)


async def _context(setup):
    factory, admin, _ = setup
    async with factory() as session:
        agent = Agent(name="binding-agent", created_by=admin.id)
        direct_instance = Instance(
            cluster_id="direct-cluster",
            name="direct",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="direct.example.invalid",
            port=3306,
        )
        backend_instance = Instance(
            cluster_id="backend-cluster",
            name="backend",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="backend.example.invalid",
            port=3306,
        )
        session.add_all([agent, direct_instance, backend_instance])
        await session.flush()
        direct_credential = InstanceCredential(
            instance_id=direct_instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("secret"),
            database_name="app",
            created_by_user_id=admin.id,
        )
        admin_credential = InstanceCredential(
            instance_id=backend_instance.id,
            name="ddl-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add_all([direct_credential, admin_credential])
        await session.flush()
        backend = ProvisioningBackend(
            instance_id=backend_instance.id,
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
        return agent, direct_instance, direct_credential, backend


async def test_instance_access_allows_provisioning_only_without_credential(
    client, setup
):
    http, admin_headers, _ = client
    agent, _, _, backend = await _context(setup)

    response = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": backend.instance_id,
            "credential_id": None,
            "permission": None,
            "direct_enabled": None,
            "capabilities": ["db_instance:create"],
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["instance_id"] == backend.instance_id
    assert response.json()["credential_id"] is None
    assert response.json()["direct_binding_id"] is None
    assert response.json()["provisioning_binding_id"] is not None
    assert response.json()["capabilities"] == ["db_instance:create"]
    assert response.json()["create_availability"] == "available"


async def test_instance_access_returns_stable_provisioning_errors(
    client, setup
):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    agent, direct_instance, _, _ = await _context(setup)

    ineligible = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": direct_instance.id,
            "capabilities": ["db_instance:create"],
        },
        headers=admin_headers,
    )
    assert ineligible.status_code == 422
    assert ineligible.json()["detail"]["code"] == (
        "INSTANCE_NOT_MULTITENANT"
    )

    async with factory() as session:
        no_backend = Instance(
            cluster_id="binding-no-backend",
            name="binding-no-backend",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(no_backend)
        await session.commit()

    missing = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": no_backend.id,
            "capabilities": ["db_instance:create"],
        },
        headers=admin_headers,
    )
    assert missing.status_code == 422
    assert missing.json()["detail"] == {
        "code": "PROVISIONING_BACKEND_REQUIRED",
        "message": (
            "Configure a provisioning backend for this instance first"
        ),
    }

    direct_required = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": direct_instance.id,
            "capabilities": ["sql:read"],
        },
        headers=admin_headers,
    )
    assert direct_required.status_code == 422
    assert direct_required.json()["detail"]["code"] == (
        "DIRECT_CREDENTIAL_REQUIRED"
    )


async def test_direct_binding_expands_dependencies_and_is_audited(client, setup):
    http, admin_headers, _ = client
    factory, _, _ = setup
    agent, instance, credential, _ = await _context(setup)

    created = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": instance.id,
            "credential_id": credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:credentials:read"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )

    assert created.status_code == 201
    assert created.json()["capabilities"] == [
        "db_instance:list",
        "db_instance:describe",
        "db_instance:credentials:read",
    ]
    listed = await http.get(
        f"/api/agents/{agent.id}/instance-bindings",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert [row["instance_id"] for row in listed.json()] == [
        created.json()["instance_id"]
    ]
    assert listed.json()[0]["capabilities"] == created.json()["capabilities"]
    async with factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "binding.create",
                AuditLog.target_id == created.json()["instance_id"],
            )
        )
        assert audit is not None


async def test_direct_binding_can_be_updated_and_deleted(client, setup):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    agent, instance, credential, _ = await _context(setup)
    created = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": instance.id,
            "credential_id": credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:list"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    async with factory() as session:
        writer = InstanceCredential(
            instance_id=instance.id,
            name="writer",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("writer"),
            password_ciphertext=encrypt("secret"),
            database_name="app",
            created_by_user_id=admin.id,
        )
        session.add(writer)
        await session.commit()

    updated = await http.put(
        f"/api/agents/{agent.id}/instance-bindings/{instance.id}",
        json={
            "credential_id": writer.id,
            "permission": "readwrite",
            "capabilities": ["db_instance:describe"],
            "direct_enabled": False,
        },
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["capabilities"] == [
        "db_instance:list",
        "db_instance:describe",
    ]
    assert updated.json()["direct_enabled"] is False

    downgraded = await http.put(
        f"/api/agents/{agent.id}/instance-bindings/{instance.id}",
        json={
            "credential_id": credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:list"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert downgraded.status_code == 200
    assert downgraded.json()["capabilities"] == ["db_instance:list"]

    deleted = await http.delete(
        f"/api/agents/{agent.id}/instance-bindings/{instance.id}",
        headers=admin_headers,
    )
    assert deleted.status_code == 204
    async with factory() as session:
        direct_binding_id = created.json()["direct_binding_id"]
        assert (
            await session.get(
                AgentInstanceBinding, direct_binding_id
            )
        ) is None
        actions = list(
            (
                await session.execute(
                    select(AuditLog.action).where(
                        AuditLog.target_id == instance.id
                    )
                )
            ).scalars()
        )
        assert actions == [
            "binding.create",
            "binding.update",
            "binding.update",
            "binding.delete",
        ]


async def test_direct_binding_accepts_explicit_sql_capabilities_and_validates_permission(
    client, setup
):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    agent, instance, readonly_credential, _ = await _context(setup)

    created = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": instance.id,
            "credential_id": readonly_credential.id,
            "permission": "readonly",
            "capabilities": ["sql:read"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert created.json()["capabilities"] == ["sql:read"]

    async with factory() as session:
        writer = InstanceCredential(
            instance_id=instance.id,
            name="writer",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("writer"),
            password_ciphertext=encrypt("secret"),
            database_name="app",
            created_by_user_id=admin.id,
        )
        session.add(writer)
        await session.commit()

    updated = await http.put(
        f"/api/agents/{agent.id}/instance-bindings/{instance.id}",
        json={
            "credential_id": writer.id,
            "permission": "readwrite",
            "capabilities": ["sql:write"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["capabilities"] == [
        "sql:read",
        "sql:write",
    ]

    rejected = await http.put(
        f"/api/agents/{agent.id}/instance-bindings/{instance.id}",
        json={
            "credential_id": readonly_credential.id,
            "permission": "readonly",
            "capabilities": ["sql:write"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == (
        "sql:write requires readwrite binding permission"
    )


async def test_direct_binding_rejects_credential_mismatch_and_escalation(client, setup):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    agent, instance, readonly_credential, _ = await _context(setup)
    async with factory() as session:
        other_instance = Instance(
            cluster_id="other",
            name="other",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(other_instance)
        await session.flush()
        other_credential = InstanceCredential(
            instance_id=other_instance.id,
            name="other",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("writer"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add(other_credential)
        await session.commit()

    mismatch = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": instance.id,
            "credential_id": other_credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:list"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert mismatch.status_code == 422

    escalation = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": instance.id,
            "credential_id": readonly_credential.id,
            "permission": "readwrite",
            "capabilities": ["db_instance:list"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert escalation.status_code == 422


async def test_provisioning_binding_requires_active_backend_and_guards_delete(client, setup):
    http, admin_headers, _ = client
    factory, _, _ = setup
    agent, _, _, backend = await _context(setup)

    created = await http.post(
        f"/api/agents/{agent.id}/provisioning-bindings",
        json={"backend_id": backend.id, "enabled": True},
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert created.json()["allow_create"] is True
    binding_id = created.json()["id"]
    listed = await http.get(
        f"/api/agents/{agent.id}/provisioning-bindings",
        headers=admin_headers,
    )
    assert [row["id"] for row in listed.json()] == [binding_id]

    async with factory() as session:
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="active-resource",
            request_fingerprint="0" * 64,
            engine=InstanceEngine.POLARDB_MYSQL,
            status=DBInstanceStatus.READY,
        )
        session.add(resource)
        await session.commit()
        resource_id = resource.id

    blocked = await http.delete(
        f"/api/agents/{agent.id}/provisioning-bindings/{binding_id}",
        headers=admin_headers,
    )
    assert blocked.status_code == 409
    disabled = await http.put(
        f"/api/agents/{agent.id}/provisioning-bindings/{binding_id}",
        json={"enabled": False},
        headers=admin_headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["allow_create"] is False

    async with factory() as session:
        row = await session.get(ProvisioningBackend, backend.id)
        assert row is not None
        row.status = ProvisioningBackendStatus.DRAINING
        await session.commit()
    rejected = await http.put(
        f"/api/agents/{agent.id}/provisioning-bindings/{binding_id}",
        json={"enabled": True},
        headers=admin_headers,
    )
    assert rejected.status_code == 422
    async with factory() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource is not None
        resource.status = DBInstanceStatus.DELETED
        await session.commit()
    deleted = await http.delete(
        f"/api/agents/{agent.id}/provisioning-bindings/{binding_id}",
        headers=admin_headers,
    )
    assert deleted.status_code == 204
    async with factory() as session:
        assert (await session.get(AgentProvisioningBinding, binding_id)) is None


async def test_agent_resources_exclude_deleted_and_are_scoped(client, setup):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    agent, _, _, backend = await _context(setup)
    async with factory() as session:
        other = Agent(name="other-agent", created_by=admin.id)
        session.add(other)
        await session.flush()
        session.add_all(
            [
                DBInstanceResource(
                    owner_agent_id=agent.id,
                    backend_id=backend.id,
                    client_token="ready",
                    request_fingerprint="1" * 64,
                    engine=InstanceEngine.POLARDB_MYSQL,
                    status=DBInstanceStatus.READY,
                ),
                DBInstanceResource(
                    owner_agent_id=agent.id,
                    backend_id=backend.id,
                    client_token="deleted",
                    request_fingerprint="2" * 64,
                    engine=InstanceEngine.POLARDB_MYSQL,
                    status=DBInstanceStatus.DELETED,
                ),
                DBInstanceResource(
                    owner_agent_id=other.id,
                    backend_id=backend.id,
                    client_token="other",
                    request_fingerprint="3" * 64,
                    engine=InstanceEngine.POLARDB_MYSQL,
                    status=DBInstanceStatus.READY,
                ),
            ]
        )
        await session.commit()

    response = await http.get(f"/api/agents/{agent.id}/resources", headers=admin_headers)
    assert response.status_code == 200
    assert [item["client_token"] for item in response.json()] == ["ready"]


async def test_binding_routes_are_admin_only(client, setup):
    http, _, member_headers = client
    agent, _, _, _ = await _context(setup)

    assert (
        await http.get(
            f"/api/agents/{agent.id}/instance-bindings",
            headers=member_headers,
        )
    ).status_code == 403
    assert (
        await http.get(
            f"/api/agents/{agent.id}/provisioning-bindings",
            headers=member_headers,
        )
    ).status_code == 403
    assert (
        await http.get(
            f"/api/agents/{agent.id}/resources",
            headers=member_headers,
        )
    ).status_code == 403


async def test_required_binding_audit_failure_rolls_back_mutation(client, setup, monkeypatch):
    http, admin_headers, _ = client
    factory, _, _ = setup
    agent, instance, credential, _ = await _context(setup)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.agent_bindings.log_audit", fail_audit)
    response = await http.post(
        f"/api/agents/{agent.id}/instance-bindings",
        json={
            "instance_id": instance.id,
            "credential_id": credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:list"],
            "direct_enabled": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 503
    async with factory() as session:
        assert (await session.execute(select(AgentInstanceBinding))).scalar_one_or_none() is None
