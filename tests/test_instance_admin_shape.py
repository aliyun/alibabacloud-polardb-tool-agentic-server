from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Delete

from server.models import (
    Agent,
    AgentInstanceBinding,
    AllocationMode,
    AuditLog,
    AuditStatus,
    CredentialCapability,
    CredentialPurpose,
    Department,
    DepartmentInstanceBinding,
    InstanceCredential,
    InstanceTopology,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    User,
    UserInstanceBinding,
    Base,
)
from server.core import instance_manager
from server.db.engine import enable_sqlite_foreign_keys

pytest_plugins = ("tests._admin_api_fixtures",)

_REGISTERED_CONNECTION = {
    "host": "db.example.invalid",
    "port": 3306,
    "username": "proxy_user",
    "password": "proxy_password",
}


@pytest.fixture(autouse=True)
def _stub_registered_connection(monkeypatch):
    async def healthy_connection(**_kwargs):
        return None

    monkeypatch.setattr(
        "server.core.instance_connection.test_mysql_connection",
        healthy_connection,
    )


async def test_registration_uses_engine_topology_and_allocation_mode(
    client,
):
    http, admin_headers, _ = client

    response = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-registered",
            "name": "Registered production",
            "engine": "polardb_mysql",
            "topology": "multitenant",
            "region": "cn-hangzhou",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["engine"] == "polardb_mysql"
    assert response.json()["topology"] == "multitenant"
    assert response.json()["allocation_mode"] == "registered"
    assert "type" not in response.json()


async def test_usage_is_returned_updated_and_cleared(
    client,
):
    http, admin_headers, _ = client
    created = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-usage",
            "name": "Usage metadata",
            "usage": "  Finance reporting  ",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )

    assert created.status_code == 201
    instance_id = created.json()["id"]
    assert created.json()["usage"] == "Finance reporting"

    listed = await http.get("/api/instances", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["usage"] == "Finance reporting"

    detailed = await http.get(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )
    assert detailed.status_code == 200
    assert detailed.json()["usage"] == "Finance reporting"

    updated = await http.put(
        f"/api/instances/{instance_id}",
        json={"usage": "  Disaster recovery  "},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["usage"] == "Disaster recovery"

    cleared = await http.put(
        f"/api/instances/{instance_id}",
        json={"usage": "   "},
        headers=admin_headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["usage"] is None


async def test_registration_rejects_legacy_type(client):
    http, admin_headers, _ = client

    response = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-legacy",
            "name": "Legacy",
            "type": "shared",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_registration_requires_explicit_instance_dimensions(client):
    http, admin_headers, _ = client

    response = await http.post(
        "/api/instances",
        json={"cluster_id": "pc-implicit", "name": "Implicit"},
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_registration_rejects_unsupported_allocation_mode(client):
    http, admin_headers, _ = client

    response = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-auto",
            "name": "Auto",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            "allocation_mode": "auto_provisioned",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_registration_and_update_validate_endpoint_fields(client):
    http, admin_headers, _ = client

    invalid_create = await http.post(
        "/api/instances",
        json={
            "cluster_id": "",
            "name": "x" * 256,
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
            "host": "",
            "port": 70000,
        },
        headers=admin_headers,
    )
    assert invalid_create.status_code == 422

    created = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-update-validation",
            "name": "Valid",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    invalid_update = await http.put(
        f"/api/instances/{created.json()['id']}",
        json={"name": "", "port": 0, "unknown": True},
        headers=admin_headers,
    )
    assert invalid_update.status_code == 422
    for payload in ({}, {"name": None}):
        response = await http.put(
            f"/api/instances/{created.json()['id']}",
            json=payload,
            headers=admin_headers,
        )
        assert response.status_code == 422
    endpoint_without_credential = await http.put(
        f"/api/instances/{created.json()['id']}",
        json={"host": "replacement.example.invalid"},
        headers=admin_headers,
    )
    assert endpoint_without_credential.status_code == 422
    assert (
        endpoint_without_credential.json()["detail"]["code"]
        == "TEST_CREDENTIAL_REQUIRED"
    )
    listed = await http.get(
        f"/api/instances/{created.json()['id']}/credentials",
        headers=admin_headers,
    )
    credential_id = listed.json()[0]["id"]
    endpoint_update = await http.put(
        f"/api/instances/{created.json()['id']}",
        json={
            "host": "replacement.example.invalid",
            "test_credential_id": credential_id,
        },
        headers=admin_headers,
    )
    assert endpoint_update.status_code == 200
    assert endpoint_update.json()["port"] == 3306

    metadata_update = await http.put(
        f"/api/instances/{created.json()['id']}",
        json={"name": "Renamed"},
        headers=admin_headers,
    )
    assert metadata_update.status_code == 200


async def test_remove_rejects_registered_instance_references_without_cascade(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, _, member = setup
    created = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-bound-delete",
            "name": "Bound",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    async with factory() as session:
        department = Department(name="Delete guard")
        session.add(department)
        await session.flush()
        binding = UserInstanceBinding(
            user_id=member.id,
            instance_id=instance_id,
            permission=Permission.READONLY,
        )
        department_binding = DepartmentInstanceBinding(
            department_id=department.id,
            instance_id=instance_id,
        )
        session.add_all([binding, department_binding])
        await session.commit()
        binding_id = binding.id
        department_binding_id = department_binding.id

    response = await http.delete(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Instance cannot be removed while it is referenced"
    }
    async with factory() as session:
        assert await session.get(UserInstanceBinding, binding_id) is not None
        assert (
            await session.get(
                DepartmentInstanceBinding,
                department_binding_id,
            )
            is not None
        )


async def test_remove_deletes_unreferenced_owned_credentials(client, setup):
    http, admin_headers, _ = client
    factory, admin, member = setup
    created = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-credential-delete",
            "name": "Credential",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    async with factory() as session:
        session.add(
            InstanceCredential(
                instance_id=instance_id,
                name="reader",
                purpose=CredentialPurpose.DIRECT_ACCESS,
                capability=CredentialCapability.READONLY,
                username_ciphertext="encrypted",
                password_ciphertext="encrypted",
                created_by_user_id=admin.id,
            )
        )
        await session.commit()

    response = await http.delete(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )

    assert response.status_code == 204
    async with factory() as session:
        assert (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.instance_id == instance_id
                )
            )
        ).scalar_one_or_none() is None


async def test_remove_rejects_non_registered_instance(client, setup):
    http, admin_headers, _ = client
    factory, _, member = setup
    from server.models import Instance, InstanceStatus, InstanceTopology

    async with factory() as session:
        instance = Instance(
            cluster_id="pc-owned-auto",
            name="Owned",
            engine="polardb_mysql",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            owner_user_id=member.id,
            status=InstanceStatus.ACTIVE,
        )
        session.add(instance)
        await session.commit()
        instance_id = instance.id

    response = await http.delete(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )

    assert response.status_code == 409


async def test_remove_preserves_audit_history(client, setup):
    http, admin_headers, _ = client
    factory, admin, member = setup
    created = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-audited-delete",
            "name": "Audited",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    async with factory() as session:
        stored_member = await session.get(User, member.id)
        assert stored_member is not None
        stored_member.default_instance_id = instance_id
        audit = AuditLog(
            actor_user_id=admin.id,
            instance_id=instance_id,
            action="instance.test",
            status=AuditStatus.SUCCESS,
        )
        explicit_audit = AuditLog(
            actor_user_id=admin.id,
            instance_id=instance_id,
            action="instance.explicit-target",
            target_type="external_instance",
            target_id="pc-audited-delete",
            status=AuditStatus.SUCCESS,
        )
        session.add_all([audit, explicit_audit])
        await session.commit()
        audit_id = audit.id
        explicit_audit_id = explicit_audit.id

    response = await http.delete(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )

    assert response.status_code == 204
    async with factory() as session:
        audit = await session.get(AuditLog, audit_id)
        assert audit is not None
        assert audit.instance_id is None
        assert audit.target_type == "instance"
        assert audit.target_id == instance_id
        explicit_audit = await session.get(AuditLog, explicit_audit_id)
        assert explicit_audit is not None
        assert explicit_audit.instance_id is None
        assert explicit_audit.target_type == "external_instance"
        assert explicit_audit.target_id == "pc-audited-delete"
        removal_audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "instance.remove",
                    AuditLog.target_id == instance_id,
                )
            )
        ).scalar_one()
        assert removal_audit.instance_id is None
        assert removal_audit.target_type == "instance"
        stored_member = await session.get(User, member.id)
        assert stored_member is not None
        assert stored_member.default_instance_id is None


async def test_remove_rolls_back_when_required_audit_fails(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    created = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-audit-failure",
            "name": "Audit failure",
            "engine": "polardb_mysql",
            "topology": "single_tenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.instances.log_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await http.delete(
            f"/api/instances/{instance_id}",
            headers=admin_headers,
        )

    async with factory() as session:
        from server.models import Instance

        assert await session.get(Instance, instance_id) is not None


async def test_instance_response_includes_health_and_binding_counts(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, admin, member = setup

    create = await http.post(
        "/api/instances",
        json={
            "cluster_id": "pc-counted",
            "name": "Counted",
            "engine": "polardb_mysql",
            "topology": "multitenant",
            **_REGISTERED_CONNECTION,
        },
        headers=admin_headers,
    )
    assert create.status_code == 201
    instance_id = create.json()["id"]

    async with factory() as session:
        department = Department(name="Engineering")
        agent = Agent(name="reporter", created_by=admin.id)
        session.add_all([department, agent])
        await session.flush()
        direct = InstanceCredential(
            instance_id=instance_id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            created_by_user_id=admin.id,
        )
        provisioning = InstanceCredential(
            instance_id=instance_id,
            name="provisioner",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            created_by_user_id=admin.id,
        )
        session.add_all([direct, provisioning])
        await session.flush()
        agent_binding = AgentInstanceBinding(
            agent_id=agent.id,
            instance_id=instance_id,
            credential_id=direct.id,
            permission=Permission.READONLY,
            created_by_user_id=admin.id,
        )
        session.add_all(
            [
                UserInstanceBinding(
                    user_id=member.id,
                    instance_id=instance_id,
                    credential_id=direct.id,
                    permission=Permission.READONLY,
                ),
                DepartmentInstanceBinding(
                    department_id=department.id,
                    instance_id=instance_id,
                ),
                agent_binding,
            ]
        )
        backend = ProvisioningBackend(
            instance_id=instance_id,
            admin_credential_id=provisioning.id,
            priority=0,
            max_active_resources=10,
            resource_min_cpu=1,
            resource_max_cpu=2,
            ddl_concurrency=1,
        )
        session.add(backend)
        await session.flush()
        session.add(
            ProvisioningBackendHealth(
                backend_id=backend.id,
                healthy=True,
                checked_at=datetime(2026, 7, 26, tzinfo=UTC),
            )
        )
        await session.commit()

    response = await http.get(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["binding_counts"] == {
        "users": 1,
        "departments": 1,
        "agents": 1,
    }
    assert response.json()["health"] == {
        "healthy": True,
        "checked_at": "2026-07-26T00:00:00",
        "consecutive_failures": 0,
        "error_code": None,
    }
    removal = await http.delete(
        f"/api/instances/{instance_id}",
        headers=admin_headers,
    )
    assert removal.status_code == 409
    async with factory() as session:
        assert (
            await session.get(AgentInstanceBinding, agent_binding.id)
            is not None
        )
        assert await session.get(ProvisioningBackend, backend.id) is not None


async def test_instance_inventory_is_paginated_stable_and_constant_query_count(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    for number in range(6):
        response = await http.post(
            "/api/instances",
            json={
                "cluster_id": f"pc-page-{number}",
                "name": f"Page {number}",
                    "engine": "polardb_mysql",
                    "topology": "single_tenant",
                    **_REGISTERED_CONNECTION,
            },
            headers=admin_headers,
        )
        assert response.status_code == 201

    statements: list[str] = []
    engine = factory.kw["bind"].sync_engine

    def record_selects(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_selects)
    try:
        first = await http.get(
            "/api/instances?offset=0&limit=2",
            headers=admin_headers,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_selects)

    assert first.status_code == 200
    body = first.json()
    assert body["total"] == 6
    assert body["offset"] == 0
    assert body["limit"] == 2
    assert len(body["items"]) == 2
    # Authentication plus inventory/page count remain constant; no relationship
    # select-in query is allowed to scale with the number of rows.
    inventory_statements = [
        statement for statement in statements if "FROM instances" in statement
    ]
    assert len(inventory_statements) == 2
    assert len(statements) == 5

    second = await http.get(
        "/api/instances?offset=2&limit=2",
        headers=admin_headers,
    )
    assert second.status_code == 200
    assert not (
        {row["id"] for row in body["items"]}
        & {row["id"] for row in second.json()["items"]}
    )


async def test_instance_pagination_bounds_are_in_openapi(client):
    http, _, _ = client
    schema = (await http.get("/openapi.json")).json()
    parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in schema["paths"]["/api/instances"]["get"]["parameters"]
    }
    assert parameters["offset"]["minimum"] == 0
    assert parameters["limit"]["minimum"] == 1
    assert parameters["limit"]["maximum"] == 200


async def test_direct_service_delete_translates_delete_integrity_error(
    tmp_path,
    monkeypatch,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/integrity.db")
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        instance = await instance_manager.register_instance(
            session,
            cluster_id="pc-integrity",
            name="Integrity",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
        )
        instance_id = instance.id
        original_execute = session.execute

        async def fail_delete(statement, *args, **kwargs):
            if isinstance(statement, Delete):
                raise IntegrityError("DELETE", {}, Exception("late reference"))
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", fail_delete)
        with pytest.raises(
            instance_manager.InstanceRemovalConflict,
            match="referenced",
        ):
            await instance_manager.remove_instance(session, instance_id)

    async with factory() as session:
        from server.models import Instance

        assert await session.get(Instance, instance_id) is not None
    await engine.dispose()


async def test_sqlite_reference_delete_race_fails_closed(
    tmp_path,
    monkeypatch,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup_session:
        user = User(external_id="race-user", display_name="Race user")
        setup_session.add(user)
        await setup_session.flush()
        instance = await instance_manager.register_instance(
            setup_session,
            cluster_id="pc-race",
            name="Race",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
        )
        user_id = user.id
        instance_id = instance.id

    async with factory() as deletion_session:
        original_execute = deletion_session.execute
        insertion_attempted = False

        async def insert_reference_before_delete(statement, *args, **kwargs):
            nonlocal insertion_attempted
            if isinstance(statement, Delete) and not insertion_attempted:
                insertion_attempted = True
                async with factory() as reference_session:
                    binding = UserInstanceBinding(
                        user_id=user_id,
                        instance_id=instance_id,
                        permission=Permission.READONLY,
                    )
                    reference_session.add(binding)
                    await reference_session.commit()
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(
            deletion_session,
            "execute",
            insert_reference_before_delete,
        )
        with pytest.raises(
            (instance_manager.InstanceRemovalConflict, OperationalError)
        ) as raised:
            await instance_manager.remove_instance(
                deletion_session,
                instance_id,
            )
        assert "database is locked" in str(raised.value) or isinstance(
            raised.value,
            instance_manager.InstanceRemovalConflict,
        )
        await deletion_session.rollback()

    assert insertion_attempted
    async with factory() as verification_session:
        from server.models import Instance

        assert await verification_session.get(Instance, instance_id) is not None
        bindings = (
            await verification_session.execute(
                select(UserInstanceBinding).where(
                    UserInstanceBinding.instance_id == instance_id
                )
            )
        ).scalars().all()
        assert bindings == []
    await engine.dispose()
