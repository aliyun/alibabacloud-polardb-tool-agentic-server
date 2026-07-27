from __future__ import annotations

from sqlalchemy import select

from server.core import provisioning_backend_service
from server.core.crypto import encrypt
from server.core.provisioning_adapter import HealthResult
from server.models import (
    Agent,
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
    ProvisioningBackendStatus,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def _backend_context(setup):
    factory, admin, _ = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="multitenant-cluster",
            name="multitenant",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="db.example.invalid",
            port=3306,
        )
        session.add(instance)
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="ddl-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()
        return instance, credential


async def test_create_backend_validates_immediately_and_audits(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance, credential = await _backend_context(setup)
    validated: list[str] = []

    async def healthy(_session, backend):
        validated.append(backend.id)
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    response = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "priority": 7,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert validated == [response.json()["id"]]
    assert response.json()["status"] == "active"
    assert response.json()["healthy"] is True
    assert response.json()["health_checked_at"] is not None
    assert response.json()["available_for_create"] is True
    async with factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "backend.activate",
                AuditLog.target_id == response.json()["id"],
            )
        )
        assert audit is not None


async def test_backend_rejects_mismatched_admin_credential(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    first, _ = await _backend_context(setup)
    factory, admin, _ = setup
    async with factory() as session:
        second = Instance(
            cluster_id="second-multitenant",
            name="second",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="db.example.invalid",
            port=3306,
        )
        session.add(second)
        await session.flush()
        credential = InstanceCredential(
            instance_id=second.id,
            name="ddl-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("root"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()

    response = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": first.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_unhealthy_backend_is_not_persisted(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance, credential = await _backend_context(setup)

    async def unhealthy(_session, _backend):
        return HealthResult(False, "AuthenticationError")

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        unhealthy,
    )
    response = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert "secret" not in response.text
    async with factory() as session:
        assert (
            await session.execute(select(ProvisioningBackend))
        ).scalar_one_or_none() is None


async def test_backend_drain_disable_and_reactivate(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance, credential = await _backend_context(setup)

    async def healthy(_session, _backend):
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    created = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    backend_id = created.json()["id"]
    drained = await http.post(
        f"/api/provisioning-backends/{backend_id}/drain",
        headers=admin_headers,
    )
    assert drained.status_code == 200
    assert drained.json()["status"] == "draining"
    disabled = await http.post(
        f"/api/provisioning-backends/{backend_id}/disable",
        headers=admin_headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    activated = await http.put(
        f"/api/provisioning-backends/{backend_id}",
        json={"status": "active", "priority": 3},
        headers=admin_headers,
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    assert activated.json()["priority"] == 3
    async with factory() as session:
        actions = list(
            (
                await session.execute(
                    select(AuditLog.action).where(
                        AuditLog.target_id == backend_id
                    )
                )
            ).scalars()
        )
    assert actions == [
        "backend.activate",
        "backend.drain",
        "backend.disable",
        "backend.activate",
    ]


async def test_disabled_backend_cannot_be_reopened_by_drain(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance, credential = await _backend_context(setup)
    validations: list[str] = []

    async def healthy(_session, backend):
        validations.append(backend.id)
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    created = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    backend_id = created.json()["id"]
    assert (
        await http.post(
            f"/api/provisioning-backends/{backend_id}/disable",
            headers=admin_headers,
        )
    ).status_code == 200

    rejected = await http.post(
        f"/api/provisioning-backends/{backend_id}/drain",
        headers=admin_headers,
    )

    assert rejected.status_code == 409
    assert validations == [backend_id]
    async with factory() as session:
        backend = await session.get(ProvisioningBackend, backend_id)
        assert backend is not None
        assert backend.status == ProvisioningBackendStatus.DISABLED
        drain_audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "backend.drain",
                    AuditLog.target_id == backend_id,
                )
            )
        ).scalars().all()
        assert drain_audits == []


async def test_disable_preserves_non_deleted_resources(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance, credential = await _backend_context(setup)

    async def healthy(_session, _backend):
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    created = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    backend_id = created.json()["id"]
    async with factory() as session:
        backend = await session.get(ProvisioningBackend, backend_id)
        assert backend is not None
        agent = Agent(name="resource-owner")
        session.add(agent)
        await session.flush()
        resource = DBInstanceResource(
            owner_agent_id=agent.id,
            backend_id=backend.id,
            client_token="admin-fixture",
            request_fingerprint="0" * 64,
            name="existing",
            engine=InstanceEngine.POLARDB_MYSQL,
            status=DBInstanceStatus.READY,
        )
        session.add(
            resource
        )
        await session.commit()
        resource_id = resource.id
    disabled = await http.post(
        f"/api/provisioning-backends/{backend_id}/disable",
        headers=admin_headers,
    )
    assert disabled.status_code == 200
    async with factory() as session:
        assert await session.get(ProvisioningBackend, backend_id) is not None
        resource = await session.get(DBInstanceResource, resource_id)
        assert resource is not None
        assert resource.status == DBInstanceStatus.READY


async def test_backend_request_validation_and_admin_enforcement(client, setup):
    http, admin_headers, member_headers = client
    instance, credential = await _backend_context(setup)
    invalid = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 0,
            "resource_min_cpu": 3,
            "resource_max_cpu": 2,
            "ddl_concurrency": 0,
        },
        headers=admin_headers,
    )
    assert invalid.status_code == 422
    assert (
        await http.get(
            "/api/provisioning-backends", headers=member_headers
        )
    ).status_code == 403


async def test_backend_update_rejects_empty_or_null_changes(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    instance, credential = await _backend_context(setup)

    async def healthy(_session, _backend):
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    created = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    backend_id = created.json()["id"]

    assert (
        await http.put(
            f"/api/provisioning-backends/{backend_id}",
            json={},
            headers=admin_headers,
        )
    ).status_code == 422


async def test_pool_affecting_updates_increment_config_revision(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    instance, credential = await _backend_context(setup)

    async def healthy(_session, _backend):
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    async def healthy_endpoint(**_kwargs):
        return None

    monkeypatch.setattr(
        "server.core.instance_connection.test_mysql_connection",
        healthy_endpoint,
    )
    created = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    backend_id = created.json()["id"]
    async with factory() as session:
        backend = await session.get(ProvisioningBackend, backend_id)
        assert backend is not None
        assert backend.config_revision == 1

    ddl_updated = await http.put(
        f"/api/provisioning-backends/{backend_id}",
        json={"ddl_concurrency": 6},
        headers=admin_headers,
    )
    assert ddl_updated.status_code == 200
    assert ddl_updated.json()["config_revision"] == 2
    priority_updated = await http.put(
        f"/api/provisioning-backends/{backend_id}",
        json={"priority": 9},
        headers=admin_headers,
    )
    assert priority_updated.status_code == 200
    assert priority_updated.json()["config_revision"] == 2

    async with factory() as session:
        replacement = InstanceCredential(
            instance_id=instance.id,
            name="replacement-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("replacement"),
            password_ciphertext=encrypt("replacement-secret"),
            created_by_user_id=admin.id,
        )
        session.add(replacement)
        await session.commit()
        replacement_id = replacement.id
    credential_updated = await http.put(
        f"/api/provisioning-backends/{backend_id}",
        json={"admin_credential_id": replacement_id},
        headers=admin_headers,
    )
    assert credential_updated.status_code == 200
    assert credential_updated.json()["config_revision"] == 3

    endpoint_updated = await http.put(
        f"/api/instances/{instance.id}",
        json={
            "host": "new-db.example.invalid",
            "port": 3307,
            "test_credential_id": replacement_id,
        },
        headers=admin_headers,
    )
    assert endpoint_updated.status_code == 200
    async with factory() as session:
        backend = await session.get(ProvisioningBackend, backend_id)
        assert backend is not None
        assert backend.config_revision == 4


async def test_config_revision_increment_uses_database_value(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance, credential = await _backend_context(setup)

    async def healthy(_session, _backend):
        return HealthResult(True)

    monkeypatch.setattr(
        "server.core.provisioning_backend_service.validate_backend_connectivity",
        healthy,
    )
    created = await http.post(
        "/api/provisioning-backends",
        json={
            "instance_id": instance.id,
            "admin_credential_id": credential.id,
            "max_active_resources": 10,
            "resource_min_cpu": 0,
            "resource_max_cpu": 2,
            "ddl_concurrency": 4,
        },
        headers=admin_headers,
    )
    backend_id = created.json()["id"]

    async with factory() as first, factory() as stale:
        first_backend = await first.get(ProvisioningBackend, backend_id)
        stale_backend = await stale.get(ProvisioningBackend, backend_id)
        assert first_backend is not None
        assert stale_backend is not None
        assert first_backend.config_revision == 1
        assert stale_backend.config_revision == 1

        await provisioning_backend_service.bump_backend_config_revision(
            first, first_backend
        )
        await first.commit()
        await provisioning_backend_service.bump_backend_config_revision(
            stale, stale_backend
        )
        await stale.commit()

    async with factory() as session:
        backend = await session.get(ProvisioningBackend, backend_id)
        assert backend is not None
        assert backend.config_revision == 3
    assert (
        await http.put(
            f"/api/provisioning-backends/{backend_id}",
            json={"admin_credential_id": None},
            headers=admin_headers,
        )
    ).status_code == 422
