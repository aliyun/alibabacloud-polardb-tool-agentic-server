from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.crypto import decrypt, encrypt
from server.models import (
    AllocationMode,
    AuditLog,
    AuthProvider,
    Base,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    SecretRevealLimit,
    User,
    UserRole,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture(autouse=True)
def allow_credential_connection(monkeypatch):
    async def succeed(**_kwargs):
        return None

    monkeypatch.setattr(
        "server.core.instance_connection.test_mysql_connection",
        succeed,
    )


async def _instance(
    setup,
    *,
    topology: InstanceTopology = InstanceTopology.SINGLE_TENANT,
) -> Instance:
    factory, _, _ = setup
    async with factory() as session:
        instance = Instance(
            cluster_id=f"cluster-{topology.value}",
            name=f"instance-{topology.value}",
            engine=InstanceEngine.POLARDB_MYSQL,
            topology=topology,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="db.example.invalid",
            port=3306,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)
        return instance


async def test_super_credential_requires_multitenant_polardb(client, setup):
    http, admin_headers, _ = client
    single = await _instance(setup)

    response = await http.post(
        f"/api/instances/{single.id}/credentials",
        json={
            "name": "ddl-admin",
            "purpose": "provisioning_admin",
            "capability": "admin",
            "username": "root",
            "password": "secret",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("purpose", "capability"),
    [
        ("provisioning_admin", "readwrite"),
        ("direct_access", "admin"),
    ],
)
async def test_credential_purpose_capability_must_match(
    client, setup, purpose, capability
):
    http, admin_headers, _ = client
    instance = await _instance(
        setup, topology=InstanceTopology.MULTITENANT
    )

    response = await http.post(
        f"/api/instances/{instance.id}/credentials",
        json={
            "name": "invalid",
            "purpose": purpose,
            "capability": capability,
            "username": "user",
            "password": "secret",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_create_and_list_credential_never_returns_plaintext(
    client, setup, caplog
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    plaintext = "credential-secret-value"

    created = await http.post(
        f"/api/instances/{instance.id}/credentials",
        json={
            "name": "reporting",
            "purpose": "direct_access",
            "capability": "readonly",
            "username": "reader",
            "password": plaintext,
            "database_name": "reporting",
        },
        headers=admin_headers,
    )

    assert created.status_code == 201
    credential_id = created.json()["id"]
    assert plaintext not in created.text
    assert "username" not in created.json()
    listed = await http.get(
        f"/api/instances/{instance.id}/credentials",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert plaintext not in listed.text
    async with factory() as session:
        row = await session.get(InstanceCredential, credential_id)
        assert row is not None
        assert row.username_ciphertext != "reader"
        assert row.password_ciphertext != plaintext
        assert decrypt(row.username_ciphertext or "") == "reader"
        assert decrypt(row.password_ciphertext or "") == plaintext
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "credential.create",
                AuditLog.target_id == credential_id,
            )
        )
        assert audit is not None
        assert plaintext not in (audit.metadata_json or "")
    assert plaintext not in caplog.text


async def test_reveal_is_no_store_rate_limited_and_audited(
    client, setup, caplog
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("secret"),
            database_name="app",
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    for _ in range(5):
        response = await http.post(
            f"/api/credentials/{credential_id}/reveal",
            json={"confirmed": True},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "username": "reader",
            "password": "secret",
            "database_name": "app",
        }
    limited = await http.post(
        f"/api/credentials/{credential_id}/reveal",
        json={"confirmed": True},
        headers=admin_headers,
    )
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "RATE_LIMITED"

    async with factory() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "credential.reveal",
                    AuditLog.target_id == credential_id,
                )
            )
        ).scalars().all()
        assert len(audits) == 5
        assert "secret" not in json.dumps(
            [row.metadata_json for row in audits]
        )
    assert "secret" not in caplog.text


async def test_reveal_audit_failure_fails_closed_without_secret(
    client, setup, monkeypatch
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("do-not-leak"),
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.credentials.log_audit", fail_audit)
    response = await http.post(
        f"/api/credentials/{credential_id}/reveal",
        json={"confirmed": True},
        headers=admin_headers,
    )
    assert response.status_code == 503
    assert "do-not-leak" not in response.text


async def test_revoke_clears_ciphertext_and_prevents_reveal(client, setup):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("secret"),
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    revoked = await http.post(
        f"/api/credentials/{credential_id}/revoke",
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert (
        await http.post(
            f"/api/credentials/{credential_id}/reveal",
            json={"confirmed": True},
            headers=admin_headers,
        )
    ).status_code == 409
    async with factory() as session:
        row = await session.get(InstanceCredential, credential_id)
        assert row is not None
        assert row.status == CredentialStatus.REVOKED
        assert row.username_ciphertext is None
        assert row.password_ciphertext is None


async def test_update_credential_rotates_secret_and_increments_version(
    client, setup
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("old-secret"),
            database_name="app",
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    response = await http.put(
        f"/api/credentials/{credential_id}",
        json={
            "expected_version": 1,
            "name": "analytics-reader",
            "capability": "readwrite",
            "username": "analytics",
            "password": "new-secret",
            "database_name": "analytics",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["id"] == credential_id
    assert response.json()["version"] == 2
    async with factory() as session:
        row = await session.get(InstanceCredential, credential_id)
        assert row is not None
        assert decrypt(row.username_ciphertext or "") == "analytics"
        assert decrypt(row.password_ciphertext or "") == "new-secret"
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "credential.update",
                AuditLog.target_id == credential_id,
            )
        )
        assert audit is not None
        assert "new-secret" not in (audit.metadata_json or "")


async def test_update_credential_omitted_password_preserves_secret(
    client, setup
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("keep-secret"),
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    response = await http.put(
        f"/api/credentials/{credential_id}",
        json={
            "expected_version": 1,
            "name": "reader",
            "capability": "readonly",
            "database_name": None,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    async with factory() as session:
        row = await session.get(InstanceCredential, credential_id)
        assert row is not None
        assert decrypt(row.username_ciphertext or "") == "reader"
        assert decrypt(row.password_ciphertext or "") == "keep-secret"


async def test_existing_credential_test_uses_stored_secret_when_omitted(
    client, setup
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("keep-secret"),
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    response = await http.post(
        f"/api/instances/{instance.id}/credentials/test-connection",
        json={
            "credential_id": credential_id,
            "expected_version": 1,
            "purpose": "direct_access",
            "capability": "readonly",
            "username": "reader",
            "database_name": None,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200


async def test_update_credential_preserves_password_whitespace(
    client, setup
):
    http, admin_headers, _ = client
    factory, _, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("old-secret"),
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    response = await http.put(
        f"/api/credentials/{credential_id}",
        json={
            "expected_version": 1,
            "name": "reader",
            "capability": "readonly",
            "username": "reader",
            "password": " rotated secret ",
            "database_name": None,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    async with factory() as session:
        row = await session.get(InstanceCredential, credential_id)
        assert row is not None
        assert decrypt(row.password_ciphertext or "") == " rotated secret "


async def test_update_credential_rejects_stale_version(client, setup):
    http, admin_headers, _ = client
    instance = await _instance(setup)
    factory, _, _ = setup
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("secret"),
            version=2,
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    response = await http.put(
        f"/api/credentials/{credential_id}",
        json={
            "expected_version": 1,
            "name": "stale",
            "capability": "readonly",
            "username": "reader",
            "database_name": None,
        },
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert (
        response.json()["detail"]["code"]
        == "CREDENTIAL_VERSION_CONFLICT"
    )


async def test_credential_routes_require_admin(client, setup):
    http, _, member_headers = client
    instance = await _instance(setup)
    assert (
        await http.get(
            f"/api/instances/{instance.id}/credentials",
            headers=member_headers,
        )
    ).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"confirmed": False},
        {"confirmed": True, "unexpected": "field"},
    ],
)
async def test_reveal_requires_strict_explicit_confirmation(
    client, setup, payload
):
    http, admin_headers, _ = client
    factory, admin, _ = setup
    instance = await _instance(setup)
    async with factory() as session:
        credential = InstanceCredential(
            instance_id=instance.id,
            name="confirmation-reader",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READONLY,
            username_ciphertext=encrypt("reader"),
            password_ciphertext=encrypt("must-not-return"),
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    kwargs = (
        {}
        if payload is None
        else {"json": payload}
    )
    response = await http.post(
        f"/api/credentials/{credential_id}/reveal",
        headers=admin_headers,
        **kwargs,
    )

    assert response.status_code == 422
    assert "must-not-return" not in response.text
    async with factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "credential.reveal",
                AuditLog.target_id == credential_id,
            )
        )
        assert audit is None
        limiter = await session.get(
            SecretRevealLimit,
            (admin.id, "credential", credential_id),
        )
        assert limiter is None


async def test_secret_reveal_budget_is_atomic_across_sessions(tmp_path):
    assert "secret_reveal_limits" in Base.metadata.tables
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'secret-rate.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(
            external_id="admin-limit",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        admin_id = admin.id

    async def consume() -> bool:
        from server.core.credential_service import (
            RevealRateLimitExceeded,
            consume_reveal_budget,
        )

        async with factory() as session:
            try:
                await consume_reveal_budget(
                    session, admin_id, "credential", "credential-1"
                )
                await session.commit()
                return True
            except RevealRateLimitExceeded:
                await session.rollback()
                return False

    results = await asyncio.gather(*(consume() for _ in range(6)))
    assert results.count(True) == 5
    assert results.count(False) == 1
    async with factory() as session:
        from server.models import SecretRevealLimit

        row = await session.get(
            SecretRevealLimit,
            (admin_id, "credential", "credential-1"),
        )
        assert row is not None
        assert row.request_count == 5
    await engine.dispose()
