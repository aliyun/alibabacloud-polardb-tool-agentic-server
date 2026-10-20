from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from server.core.crypto import encrypt
from server.enterprise_identity import etl_permissions as etl
from server.models import EnterpriseIdentitySource, EnterpriseIdentitySourceStatus, IdentitySourceProvider, SystemConfig

pytest_plugins = ("tests._admin_api_fixtures",)


async def seed(factory):
    async with factory() as session:
        source = EnterpriseIdentitySource.create(name="ETL", provider=IdentitySourceProvider.FEISHU, tenant_id="tenant")
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        source.last_error = "directory-marker"
        source.sync_worker_id = "directory-worker"
        source.sync_kind = "full"
        source.sync_warning_json = '{"directory":"marker"}'
        source.config_ciphertext = encrypt(
            json.dumps(
                {
                    "app_id": "app",
                    "app_secret": "secret",
                    "acl_membership_snapshot": {
                        "host": "localhost",
                        "port": 5432,
                        "database": "meta",
                        "username": "readonly",
                        "password": "secret",
                        "dialect": "postgresql",
                    },
                }
            )
        )
        session.add(source)
        await session.commit()
        return source.id


class GoodClient:
    def __init__(self, **kwargs):
        pass

    async def verify_connection(self, **kwargs):
        pass

    def iter_knowledge_scopes(self, **kwargs):
        return ()

    async def fetch_snapshot(self, **kwargs):
        raise AssertionError("The independent job must not fetch memberships")


@pytest.mark.parametrize("state", [EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE])
async def test_configure_preserves_directory_and_invalidates_connection(client, setup, state):
    factory, _, _ = setup
    http, headers, _ = client
    sid = await seed(factory)
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, sid)
        source.status = state
        await session.commit()
    response = await http.put(
        f"/api/identity-sources/{sid}/acl-membership-snapshot",
        headers=headers,
        json={"host": "new-host", "username": "readonly", "dialect": "postgresql"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == state.value
    assert response.json()["last_error"] == "directory-marker"
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, sid)
        assert source.sync_worker_id == "directory-worker"
        assert source.sync_warning_json == '{"directory":"marker"}'
        assert (await etl.status(session, sid))["connection"]["state"] == "UNVERIFIED"


async def test_independent_job_success_and_failure_preserve_directory(setup):
    factory, _, _ = setup
    sid = await seed(factory)

    class BadClient(GoodClient):
        async def verify_connection(self, **kwargs):
            raise RuntimeError("secret driver password")

    for client_type, expected in [(GoodClient, "SUCCEEDED"), (BadClient, "FAILED")]:
        async with factory() as session:
            source = await session.get(EnterpriseIdentitySource, sid)
            owner = await etl.claim(session, source, "sync")
            await session.commit()
        await etl.run(sid, owner, session_factory=factory, client_factory=client_type)
        async with factory() as session:
            source = await session.get(EnterpriseIdentitySource, sid)
            assert source.status == EnterpriseIdentitySourceStatus.ACTIVE
            assert source.sync_worker_id == "directory-worker"
            assert source.last_error == "directory-marker"
            assert source.sync_warning_json == '{"directory":"marker"}'
            value = await etl.status(session, sid)
            assert value["state"] == expected
            assert "secret" not in json.dumps(value)
            assert "owner" not in value
            if expected == "FAILED":
                assert value["sync"]["snapshots_processed"] == 0


async def test_lease_excludes_duplicates_and_config_save_and_recovers(setup):
    factory, _, _ = setup
    sid = await seed(factory)
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, sid)
        first = await etl.claim(session, source, "sync")
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await etl.claim(session, source, "verify")
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException):
            await etl.configuration_saved(session, source)
        row = await session.get(SystemConfig, etl.key(sid))
        value = json.loads(row.config_value)
        value["lease_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await etl.write(session, row, value)
        await session.commit()
        assert (await etl.status(session, sid))["error"] == "ETL_JOB_EXPIRED"
        second = await etl.claim(session, source, "sync")
        await session.commit()
    await etl.run(sid, first, session_factory=factory, client_factory=GoodClient)
    async with factory() as session:
        assert (await etl.status(session, sid))["state"] == "RUNNING"
    await etl.run(sid, second, session_factory=factory, client_factory=GoodClient)
    async with factory() as session:
        assert (await etl.status(session, sid))["state"] == "SUCCEEDED"


async def test_verify_and_sync_admin_routes(client, setup, monkeypatch):
    factory, _, _ = setup
    http, headers, member_headers = client
    sid = await seed(factory)
    monkeypatch.setattr(etl, "FeishuAclMembershipSnapshotClient", GoodClient)
    for operation in ["verify", "sync"]:
        url = f"/api/identity-sources/{sid}/etl-permissions/{operation}"
        assert (await http.post(url)).status_code == 401
        assert (await http.post(url, headers=member_headers)).status_code == 403
        response = await http.post(url, headers=headers)
        assert response.status_code == 202
        result = (await http.get(f"/api/identity-sources/{sid}/etl-permissions", headers=headers)).json()
        assert result["state"] == "SUCCEEDED"
        assert result["connection"]["state"] == "SUCCEEDED"
        assert "secret" not in json.dumps(result)


async def test_sync_reads_only_lazy_scope_stream_and_normalizes_tenant(setup, monkeypatch):
    factory, _, _ = setup
    sid = await seed(factory)
    events = []

    class ScopeOnlyClient(GoodClient):
        def iter_knowledge_scopes(self, *, tenant_key):
            assert tenant_key == "tenant"

            async def scopes():
                try:
                    events.append("opened")
                    yield "scope-1"
                    yield "scope-2"
                finally:
                    events.append("closed")

            return scopes()

    async def consume(session, source, scopes, *, record_identity_warning):
        assert not record_identity_warning
        assert events == []
        assert [scope async for scope in scopes] == ["scope-1", "scope-2"]
        return {"snapshots_processed": 2, "snapshots_skipped": 0, "errors": {}}

    monkeypatch.setattr(etl, "import_external_knowledge_scopes", consume)
    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, sid)
        source.tenant_id = " tenant "
        owner = await etl.claim(session, source, "sync")
        await session.commit()
    await etl.run(sid, owner, session_factory=factory, client_factory=ScopeOnlyClient)
    async with factory() as session:
        result = await etl.status(session, sid)
        assert result["state"] == "SUCCEEDED"
        assert result["sync"]["snapshots_processed"] == 2
    assert events == ["opened", "closed"]


async def test_verify_does_not_open_scope_stream(setup):
    factory, _, _ = setup
    sid = await seed(factory)

    class VerifyOnlyClient(GoodClient):
        def iter_knowledge_scopes(self, **kwargs):
            raise AssertionError("Connection verification must not read scopes")

    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, sid)
        owner = await etl.claim(session, source, "verify")
        await session.commit()
    await etl.run(sid, owner, session_factory=factory, client_factory=VerifyOnlyClient)
    async with factory() as session:
        assert (await etl.status(session, sid))["state"] == "SUCCEEDED"


async def test_failure_finalization_version_conflict_does_not_escape_or_overwrite(setup, monkeypatch):
    factory, _, _ = setup
    sid = await seed(factory)

    class BadClient(GoodClient):
        async def verify_connection(self, **kwargs):
            raise RuntimeError("private database failure")

    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, sid)
        owner = await etl.claim(session, source, "sync")
        await session.commit()

    async def conflict(session, row, value):
        assert value["state"] == "FAILED"
        raise HTTPException(status_code=409, detail="Concurrent state")

    monkeypatch.setattr(etl, "write", conflict)
    await etl.run(sid, owner, session_factory=factory, client_factory=BadClient)
    async with factory() as session:
        row = await session.get(SystemConfig, etl.key(sid))
        assert json.loads(row.config_value)["state"] == "RUNNING"
        assert (await session.get(EnterpriseIdentitySource, sid)).status == EnterpriseIdentitySourceStatus.ACTIVE
