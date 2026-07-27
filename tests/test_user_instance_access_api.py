from __future__ import annotations

from sqlalchemy import select

from server.auth.principal import Principal, PrincipalKind
from server.core.db_instance_query import query_db_instances
from server.core.crypto import encrypt
from server.models import (
    AllocationMode,
    AuditLog,
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    Permission,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def _system_access(setup):
    factory, admin, member = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="auto-user-cluster",
            name="auto-user",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            status=InstanceStatus.ACTIVE,
            owner_user_id=member.id,
            host="auto.example.invalid",
            port=3306,
        )
        session.add(instance)
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="auto-user",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("auto-user"),
            password_ciphertext=encrypt("secret"),
            database_name="app",
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.flush()
        binding = UserInstanceBinding(
            user_id=member.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
            enabled=True,
            origin=BindingOrigin.SYSTEM,
        )
        binding.capabilities = [
            UserInstanceBindingCapability(capability=BindingCapability.SQL_READ),
            UserInstanceBindingCapability(capability=BindingCapability.SQL_WRITE),
        ]
        session.add(binding)
        await session.commit()
        return member, instance, credential


async def test_system_user_sql_access_does_not_export_credentials(client, setup):
    http, admin_headers, _ = client
    user, instance, _ = await _system_access(setup)

    response = await http.get(
        f"/api/users/{user.id}/instance-access/{instance.id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["origin"] == "system"
    assert response.json()["capabilities"] == ["sql:read", "sql:write"]


async def test_admin_grant_preserves_sql_and_changes_system_origin_to_admin(client, setup):
    http, admin_headers, _ = client
    factory, _, _ = setup
    user, instance, credential = await _system_access(setup)

    response = await http.put(
        f"/api/users/{user.id}/instance-access/{instance.id}",
        json={
            "credential_id": credential.id,
            "permission": "readwrite",
            "capabilities": ["db_instance:credentials:read"],
            "enabled": True,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["origin"] == "admin"
    assert response.json()["capabilities"] == [
        "db_instance:list",
        "db_instance:describe",
        "db_instance:credentials:read",
        "sql:read",
        "sql:write",
    ]
    async with factory() as session:
        row = await session.scalar(
            select(UserInstanceBinding).where(
                UserInstanceBinding.user_id == user.id,
                UserInstanceBinding.instance_id == instance.id,
            )
        )
        assert row is not None
        assert row.origin == BindingOrigin.ADMIN
        assert {item.capability for item in row.capabilities} == {
            BindingCapability.DB_INSTANCE_LIST,
            BindingCapability.DB_INSTANCE_DESCRIBE,
            BindingCapability.DB_INSTANCE_CREDENTIALS_READ,
            BindingCapability.SQL_READ,
            BindingCapability.SQL_WRITE,
        }
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "binding.update",
                AuditLog.target_id == row.id,
            )
        )
        assert audit is not None
        visible = await query_db_instances(
            session,
            Principal(PrincipalKind.USER, user.id),
            limit=50,
        )
        assert len(visible.instances) == 1
        assert visible.instances[0].source == "auto_provisioned"
        assert visible.instances[0].capabilities == (
            "list",
            "describe",
            "credentials_read",
            "run_sql_read",
            "run_sql_write",
        )


async def test_user_access_put_creates_explicit_admin_binding(client, setup):
    http, admin_headers, _ = client
    factory, admin, member = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="registered-user-cluster",
            name="registered-user",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add(instance)
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="registered-reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()

    response = await http.put(
        f"/api/users/{member.id}/instance-access/{instance.id}",
        json={
            "credential_id": credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:describe"],
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["origin"] == "admin"
    assert response.json()["capabilities"] == [
        "db_instance:list",
        "db_instance:describe",
    ]


async def test_user_access_rejects_sql_escalation_above_credential(client, setup):
    http, admin_headers, _ = client
    factory, _, _ = setup
    user, instance, credential = await _system_access(setup)
    async with factory() as session:
        row = await session.get(InstanceCredential, credential.id)
        assert row is not None
        row.capability = CredentialCapability.READONLY
        await session.commit()

    response = await http.put(
        f"/api/users/{user.id}/instance-access/{instance.id}",
        json={
            "credential_id": credential.id,
            "permission": "readwrite",
            "capabilities": ["sql:write"],
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_user_access_routes_require_admin(client, setup):
    http, _, member_headers = client
    user, instance, _ = await _system_access(setup)
    assert (
        await http.get(
            f"/api/users/{user.id}/instance-access/{instance.id}",
            headers=member_headers,
        )
    ).status_code == 403


async def test_required_user_access_audit_failure_rolls_back(client, setup, monkeypatch):
    http, admin_headers, _ = client
    factory, _, _ = setup
    user, instance, credential = await _system_access(setup)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.user_instance_access.log_audit", fail_audit)
    response = await http.put(
        f"/api/users/{user.id}/instance-access/{instance.id}",
        json={
            "credential_id": credential.id,
            "permission": "readonly",
            "capabilities": ["db_instance:list"],
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 503
    async with factory() as session:
        row = await session.scalar(
            select(UserInstanceBinding).where(
                UserInstanceBinding.user_id == user.id,
                UserInstanceBinding.instance_id == instance.id,
            )
        )
        assert row is not None
        assert row.permission == Permission.READWRITE
        assert {item.capability for item in row.capabilities} == {
            BindingCapability.SQL_READ,
            BindingCapability.SQL_WRITE,
        }
