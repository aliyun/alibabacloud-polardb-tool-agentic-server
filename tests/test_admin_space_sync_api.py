from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.crypto import encrypt
from server.models import (
    AuthProvider,
    Base,
    KnowledgeResource,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
    UserRole,
)
from server.polarrag.client import client_from_instance

pytest_plugins = ("tests._admin_api_fixtures",)
URL = "/api/v1/admin/spaces/space-a/sync"


@pytest.fixture
async def setup(tmp_path):
    from server.db import engine as engine_mod
    from server.db.engine import enable_sqlite_foreign_keys

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory
    async with factory() as session:
        admin = User(external_id="admin", display_name="Admin", auth_provider=AuthProvider.BUILTIN, role=UserRole.ADMIN)
        member = User(
            external_id="member", display_name="Member", auth_provider=AuthProvider.BUILTIN, role=UserRole.MEMBER
        )
        session.add_all([admin, member])
        await session.commit()
    yield factory, admin, member
    await engine.dispose()


@pytest.fixture
async def catalog(setup, monkeypatch):
    factory, admin, _ = setup
    async with factory() as session:
        instance = PolarRAGInstance(
            name="RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext=encrypt("user"),
            password_ciphertext=encrypt("password"),
            status=PolarRAGInstanceStatus.ACTIVE,
            plugin_version="1.0.8",
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="space-a",
            name="Space A",
            identity_domain="tenant-a",
            enabled=True,
        )
        session.add(space)
        await session.commit()
        instance_id = instance.id
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        entered.set()
        await release.wait()
        kb_id = "b" if body.get("cursor") else "a"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "kb_id": kb_id,
                        "name": kb_id,
                        "kb_type": "PUBLIC",
                        "space_id": "space-a",
                        "updated_at": "2026-09-17T00:00:00Z",
                        "owner": None,
                        "identity_domain": "tenant-a",
                        "status": "ACTIVE",
                    }
                ],
                "has_more": kb_id == "a",
                "next_cursor": "a" if kb_id == "a" else None,
            },
        )

    monkeypatch.setattr(
        "server.polarrag.catalog.client_from_instance",
        lambda instance: client_from_instance(instance, transport=httpx.MockTransport(handler)),
    )
    # The existing worker captures its default factory at import time.
    from server.polarrag import catalog as service

    original = service.run_claimed_space_catalog_sync

    async def run(*args, **kwargs):
        return await original(*args, **kwargs, client_factory=service.client_from_instance)

    monkeypatch.setattr("server.api.polarrag.run_claimed_space_catalog_sync", run)
    yield calls, entered, release, instance_id
    release.set()


async def wait_done(http, headers):
    for _ in range(200):
        response = await http.get(URL, headers=headers)
        if response.json().get("status") in {"completed", "failed"}:
            return response.json()
        await asyncio.sleep(0.01)
    pytest.fail("Sync did not finish")


@pytest.mark.parametrize("body", [None, {}, {"kb_ids": ["a", "b", "a"]}])
async def test_sync_admin_auth_pagination_and_committed_completion(client, setup, catalog, body):
    http, admin, member = client
    factory, _, _ = setup
    calls, entered, release, instance_id = catalog
    assert (await http.post(URL)).status_code == 401
    assert (await http.post(URL, headers=member)).status_code == 403
    assert (await http.get(URL, headers=member)).status_code == 403
    response = await http.post(URL, headers=admin, **({"json": body} if body is not None else {}))
    assert response.status_code == 202
    assert response.json()["status"] == "running"
    await asyncio.wait_for(entered.wait(), 2)
    assert (await http.post(URL, headers=admin)).status_code == 409
    legacy = await http.post(f"/api/polarrag/instances/{instance_id}/spaces/space-a/sync", headers=admin)
    assert legacy.status_code == 202
    release.set()
    status = await wait_done(http, admin)
    assert status["status"] == "completed", status
    assert status["result"]["knowledge_bases"] == 2
    expected = {"size": 100, "statuses": ["ACTIVE"]}
    if body and "kb_ids" in body:
        expected["kb_ids"] = ["a", "b"]
    assert calls == [expected, {**expected, "cursor": "a"}]
    async with factory() as session:
        assert set(await session.scalars(select(KnowledgeResource.kb_id))) == {"a", "b"}


@pytest.mark.parametrize(
    "body",
    [
        {"kb_ids": []},
        {"kb_ids": [""]},
        {"kb_ids": [" "]},
        {"kb_ids": [1]},
        {"kb_ids": "a"},
        {"kb_ids": None},
        {"kb_ids": ["a"] * 101},
        {"unknown": 1},
    ],
)
async def test_invalid_sync_parameters_rejected_before_upstream(client, catalog, body):
    http, admin, _ = client
    calls, _, _, _ = catalog
    assert (await http.post(URL, headers=admin, json=body)).status_code == 422
    assert calls == []


async def test_unknown_space(client):
    http, admin, _ = client
    assert (await http.post(URL, headers=admin)).status_code == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("second_enabled", [True, False])
async def test_ambiguous_space_does_not_choose_an_instance_or_claim_sync(
    client, setup, catalog, method, second_enabled
):
    http, headers, _ = client
    factory, admin, _ = setup
    calls, _, release, _ = catalog
    release.set()
    async with factory() as session:
        instance = PolarRAGInstance(
            name="Duplicate RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext=encrypt("user"),
            password_ciphertext=encrypt("password"),
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        session.add(
            PolarRAGSpace(
                polarrag_instance_id=instance.id,
                space_id="space-a",
                name="Duplicate Space",
                identity_domain="tenant-a",
                enabled=second_enabled,
            )
        )
        await session.commit()
    response = await http.request(method, URL, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AMBIGUOUS_SPACE"
    assert calls == []
    async with factory() as session:
        spaces = list(await session.scalars(select(PolarRAGSpace)))
        assert len(spaces) == 2
        assert all(space.catalog_sync_status == "idle" and space.catalog_sync_worker_id is None for space in spaces)


@pytest.mark.parametrize(
    "version", ["1.0.6", "1.0.7", "t-polardb-rag-1.0.7-20260915022500.al8.x86_64", None, "invalid"]
)
async def test_legacy_version_ignores_kb_ids_and_performs_full_sync(client, setup, catalog, version):
    http, admin, _ = client
    factory, _, _ = setup
    calls, _, release, instance_id = catalog
    async with factory() as session:
        instance = await session.get(PolarRAGInstance, instance_id)
        instance.plugin_version = version
        space = await session.scalar(select(PolarRAGSpace))
        session.add(
            KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=instance_id,
                space_id="space-a",
                kb_id="retired",
                name="Retired",
                kb_type="PUBLIC",
                sync_status="active",
                identity_domain="tenant-a",
                enabled=True,
            )
        )
        await session.commit()
    release.set()
    response = await http.post(URL, headers=admin, json={"kb_ids": ["missing"]})
    assert response.status_code == 202
    status = await wait_done(http, admin)
    assert status["status"] == "completed", status
    assert status["result"]["knowledge_bases"] == 3
    assert status["result"]["disabled"] == 1
    assert calls == [{"size": 100, "statuses": ["ACTIVE"]}, {"size": 100, "statuses": ["ACTIVE"], "cursor": "a"}]
    async with factory() as session:
        space = await session.scalar(select(PolarRAGSpace))
        assert space.last_synced_at is not None
        retired = await session.scalar(select(KnowledgeResource).where(KnowledgeResource.kb_id == "retired"))
        assert retired.enabled is False


@pytest.mark.parametrize(
    "kb_ids,error",
    [
        (["a", "b", "missing"], "POLARRAG_CATALOG_TARGETS_MISSING"),
        (["a"], "POLARRAG_INVALID_RESPONSE"),
    ],
)
async def test_missing_or_unexpected_target_rolls_back_all_updates(client, setup, catalog, kb_ids, error):
    http, admin, _ = client
    factory, _, _ = setup
    _, _, release, _ = catalog
    release.set()
    response = await http.post(URL, headers=admin, json={"kb_ids": kb_ids})
    assert response.status_code == 202
    status = await wait_done(http, admin)
    assert status["status"] == "failed"
    assert status["error"] == error
    async with factory() as session:
        assert list(await session.scalars(select(KnowledgeResource))) == []
