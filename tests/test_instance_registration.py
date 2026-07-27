from __future__ import annotations

from sqlalchemy import select

from server.core.crypto import decrypt
from server.models import (
    AllocationMode,
    CredentialCapability,
    CredentialPurpose,
    Department,
    DepartmentInstanceBinding,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
)

pytest_plugins = ("tests._admin_api_fixtures",)


_ENABLE_MULTITENANT_SQL = "SHOW VARIABLES LIKE 'enable_multi_tenant'"
_RDS_KILL_USER_LIST_SQL = "SHOW VARIABLES LIKE 'rds_kill_user_list'"
_MULTITENANT_RESULTS: dict[str, tuple | None] = {
    "SELECT 1": (1,),
    _ENABLE_MULTITENANT_SQL: ("enable_multi_tenant", "ON"),
    _RDS_KILL_USER_LIST_SQL: (
        "rds_kill_user_list",
        "proxy_user, sp",
    ),
}


class _Cursor:
    def __init__(
        self,
        results: dict[str, tuple | None] | None = None,
    ) -> None:
        self.results = results or dict(_MULTITENANT_RESULTS)
        self.executed: list[str] = []
        self.current_sql: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)
        self.current_sql = sql

    async def fetchone(self) -> tuple | None:
        assert self.current_sql is not None
        return self.results.get(self.current_sql)


class _Connection:
    def __init__(
        self,
        results: dict[str, tuple | None] | None = None,
    ) -> None:
        self.cursor_instance = _Cursor(results)
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    async def ensure_closed(self) -> None:
        self.closed = True


def _registration_payload(topology: str = "single_tenant") -> dict:
    return {
        "cluster_id": f"pc-{topology}",
        "name": f"Registered {topology}",
        "engine": "polardb_mysql",
        "topology": topology,
        "region": "cn-hangzhou",
        "host": "db.example.invalid",
        "port": 3306,
        "username": "proxy_user",
        "password": "proxy_password",
    }


async def test_connection_preflight_executes_select_one_and_closes(
    client,
    monkeypatch,
):
    http, admin_headers, _ = client
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances/test-connection",
        json={
            "topology": "single_tenant",
            "host": "db.example.invalid",
            "port": 3306,
            "username": "proxy_user",
            "password": "proxy_password",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert connection.cursor_instance.executed == ["SELECT 1"]
    assert connection.closed is True


async def test_multitenant_connection_preflight_checks_cluster_and_account(
    client,
    monkeypatch,
):
    http, admin_headers, _ = client
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances/test-connection",
        json={
            "topology": "multitenant",
            "host": "db.example.invalid",
            "port": 3306,
            "username": "proxy_user",
            "password": "proxy_password",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert connection.cursor_instance.executed == [
        "SELECT 1",
        _ENABLE_MULTITENANT_SQL,
        _RDS_KILL_USER_LIST_SQL,
    ]
    assert connection.closed is True


async def test_multitenant_connection_rejects_disabled_cluster(
    client,
    monkeypatch,
):
    http, admin_headers, _ = client
    results = dict(_MULTITENANT_RESULTS)
    results[_ENABLE_MULTITENANT_SQL] = ("enable_multi_tenant", "OFF")
    connection = _Connection(results)

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances/test-connection",
        json={
            "topology": "multitenant",
            "host": "db.example.invalid",
            "port": 3306,
            "username": "proxy_user",
            "password": "proxy_password",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MULTITENANT_DISABLED"
    assert connection.closed is True


async def test_multitenant_connection_requires_listed_admin(
    client,
    monkeypatch,
):
    http, admin_headers, _ = client
    results = dict(_MULTITENANT_RESULTS)
    results[_RDS_KILL_USER_LIST_SQL] = (
        "rds_kill_user_list",
        "sp, another_admin",
    )
    connection = _Connection(results)

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances/test-connection",
        json={
            "topology": "multitenant",
            "host": "db.example.invalid",
            "port": 3306,
            "username": "proxy_user",
            "password": "proxy_password",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]["code"]
        == "MULTITENANT_ADMIN_REQUIRED"
    )
    assert connection.closed is True


async def test_multitenant_connection_fails_closed_for_invalid_variable(
    client,
    monkeypatch,
):
    http, admin_headers, _ = client
    results = dict(_MULTITENANT_RESULTS)
    results[_ENABLE_MULTITENANT_SQL] = None
    connection = _Connection(results)

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances/test-connection",
        json={
            "topology": "multitenant",
            "host": "db.example.invalid",
            "port": 3306,
            "username": "proxy_user",
            "password": "proxy_password",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]["code"]
        == "MULTITENANT_PREFLIGHT_FAILED"
    )
    assert connection.closed is True


async def test_direct_credential_connection_uses_declared_database(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="pc-direct-credential-test",
            name="Direct credential test",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="db.example.invalid",
            port=3306,
        )
        session.add(instance)
        await session.commit()
        instance_id = instance.id

    connection = _Connection()
    connect_kwargs: dict = {}

    async def connect(**kwargs):
        connect_kwargs.update(kwargs)
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        f"/api/instances/{instance_id}/credentials/test-connection",
        json={
            "purpose": "direct_access",
            "capability": "readonly",
            "username": "reader",
            "password": "secret",
            "database_name": "analytics",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert connect_kwargs["db"] == "analytics"
    assert connection.cursor_instance.executed == ["SELECT 1"]
    assert connection.closed is True


async def test_failed_credential_connection_does_not_persist_credential(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    async with factory() as session:
        instance = Instance(
            cluster_id="pc-rejected-credential",
            name="Rejected credential",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="db.example.invalid",
            port=3306,
        )
        session.add(instance)
        await session.commit()
        instance_id = instance.id

    async def connect(**_kwargs):
        raise OSError("private driver detail")

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        f"/api/instances/{instance_id}/credentials",
        json={
            "name": "unreachable",
            "purpose": "direct_access",
            "capability": "readonly",
            "username": "reader",
            "password": "secret",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CONNECTION_TEST_FAILED"
    assert "private driver detail" not in response.text
    async with factory() as session:
        assert (
            await session.execute(select(InstanceCredential))
        ).scalar_one_or_none() is None


async def test_multitenant_registration_rechecks_preflight(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    results = dict(_MULTITENANT_RESULTS)
    results[_ENABLE_MULTITENANT_SQL] = ("enable_multi_tenant", "OFF")
    connection = _Connection(results)

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances",
        json=_registration_payload("multitenant"),
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "MULTITENANT_DISABLED"
    assert connection.closed is True
    async with factory() as session:
        assert (
            await session.execute(select(Instance))
        ).scalar_one_or_none() is None
        assert (
            await session.execute(select(InstanceCredential))
        ).scalar_one_or_none() is None


async def test_registration_requires_complete_connection_tuple(client):
    http, admin_headers, _ = client
    payload = _registration_payload()

    for missing in ("host", "port", "username", "password"):
        invalid = dict(payload)
        invalid.pop(missing)
        response = await http.post(
            "/api/instances",
            json=invalid,
            headers=admin_headers,
        )
        assert response.status_code == 422


async def test_registration_retests_and_persists_encrypted_direct_credential(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    connection = _Connection()

    async def connect(**_kwargs):
        return connection

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances",
        json=_registration_payload(),
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["allocation_mode"] == "registered"
    async with factory() as session:
        instance = (
            await session.execute(select(Instance))
        ).scalar_one()
        credential = (
            await session.execute(select(InstanceCredential))
        ).scalar_one()
        assert credential.instance_id == instance.id
        assert credential.purpose == CredentialPurpose.DIRECT_ACCESS
        assert credential.capability == CredentialCapability.READWRITE
        assert decrypt(credential.username_ciphertext or "") == "proxy_user"
        assert decrypt(credential.password_ciphertext or "") == (
            "proxy_password"
        )


async def test_registration_normalizes_and_persists_usage(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup

    async def connect(**_kwargs):
        return _Connection()

    monkeypatch.setattr("asyncmy.connect", connect)
    payload = _registration_payload()
    payload["usage"] = "  Finance reporting  "

    response = await http.post(
        "/api/instances",
        json=payload,
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["usage"] == "Finance reporting"
    async with factory() as session:
        instance = (
            await session.execute(select(Instance))
        ).scalar_one()
        assert instance.usage == "Finance reporting"


async def test_registration_rejects_usage_over_1024_characters(
    client,
    monkeypatch,
):
    http, admin_headers, _ = client

    async def connect(**_kwargs):
        return _Connection()

    monkeypatch.setattr("asyncmy.connect", connect)
    payload = _registration_payload()
    payload["usage"] = "x" * 1025

    response = await http.post(
        "/api/instances",
        json=payload,
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_multitenant_registration_creates_provisioning_admin_credential(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup

    async def connect(**_kwargs):
        return _Connection()

    monkeypatch.setattr("asyncmy.connect", connect)
    response = await http.post(
        "/api/instances",
        json=_registration_payload("multitenant"),
        headers=admin_headers,
    )

    assert response.status_code == 201
    async with factory() as session:
        credential = (
            await session.execute(select(InstanceCredential))
        ).scalar_one()
        assert credential.purpose == CredentialPurpose.PROVISIONING_ADMIN
        assert credential.capability == CredentialCapability.ADMIN


async def test_registration_preserves_password_whitespace(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    connected_passwords: list[str] = []

    async def connect(**kwargs):
        connected_passwords.append(kwargs["password"])
        return _Connection()

    monkeypatch.setattr("asyncmy.connect", connect)
    payload = _registration_payload()
    payload["password"] = " proxy password "
    response = await http.post(
        "/api/instances",
        json=payload,
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert connected_passwords == [" proxy password "]
    async with factory() as session:
        credential = (
            await session.execute(select(InstanceCredential))
        ).scalar_one()
        assert decrypt(credential.password_ciphertext or "") == (
            " proxy password "
        )


async def test_failed_registration_does_not_persist_partial_records(
    client,
    setup,
    monkeypatch,
):
    http, admin_headers, _ = client
    factory, _, _ = setup

    async def fail_connect(**_kwargs):
        raise OSError("secret-bearing network failure")

    monkeypatch.setattr("asyncmy.connect", fail_connect)
    response = await http.post(
        "/api/instances",
        json=_registration_payload(),
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert "secret-bearing" not in response.text
    async with factory() as session:
        assert (
            await session.execute(select(Instance))
        ).scalar_one_or_none() is None
        assert (
            await session.execute(select(InstanceCredential))
        ).scalar_one_or_none() is None


async def test_department_binds_an_existing_active_multitenant_instance(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    async with factory() as session:
        department = Department(name="Finance")
        instance = Instance(
            cluster_id="pc-shared",
            name="Shared multitenant",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="db.example.invalid",
            port=3306,
        )
        session.add_all([department, instance])
        await session.commit()
        department_id = department.id
        instance_id = instance.id

    response = await http.post(
        f"/api/departments/{department_id}/multitenant-instance",
        json={"instance_id": instance_id},
        headers=admin_headers,
    )

    assert response.status_code == 201
    async with factory() as session:
        binding = (
            await session.execute(select(DepartmentInstanceBinding))
        ).scalar_one()
        assert binding.department_id == department_id
        assert binding.instance_id == instance_id


async def test_department_rejects_non_multitenant_or_second_instance(
    client,
    setup,
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    async with factory() as session:
        department = Department(name="Engineering")
        single = Instance(
            cluster_id="pc-single",
            name="Single",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        first = Instance(
            cluster_id="pc-multi-first",
            name="First",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        second = Instance(
            cluster_id="pc-multi-second",
            name="Second",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
        )
        session.add_all([department, single, first, second])
        await session.commit()
        department_id = department.id
        single_id = single.id
        first_id = first.id
        second_id = second.id

    rejected_single = await http.post(
        f"/api/departments/{department_id}/multitenant-instance",
        json={"instance_id": single_id},
        headers=admin_headers,
    )
    assert rejected_single.status_code == 400

    first_binding = await http.post(
        f"/api/departments/{department_id}/multitenant-instance",
        json={"instance_id": first_id},
        headers=admin_headers,
    )
    assert first_binding.status_code == 201

    rejected_second = await http.post(
        f"/api/departments/{department_id}/multitenant-instance",
        json={"instance_id": second_id},
        headers=admin_headers,
    )
    assert rejected_second.status_code == 400
