from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

from server.core.crypto import decrypt, encrypt
from server.app import create_app
from server.api.polarrag import _find_knowledge_base, _find_upstream_space
from server.models import (
    AuditLog,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeResource,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    AuthProvider,
    User,
)
from server.polarrag.contracts import (
    PolarRAGCapabilities,
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGSpaceRecord,
    PolarRAGUpstreamError,
)
from server.polarrag.catalog import (
    _run_claimed_space_catalog_sync,
    claim_space_catalog_sync,
)

pytest_plugins = ("tests._admin_api_fixtures",)


class FakeAdminClient:
    def __init__(self) -> None:
        self.oss_bucket = "tenant-a-documents"
        self.oss_endpoint = "oss-cn-hangzhou.aliyuncs.com"

    async def check_capabilities(self):
        return PolarRAGCapabilities(
            version="5faf397",
            search=True,
            protected_document_info=True,
            protected_context=True,
            protected_document_search=True,
            space_catalog=True,
            knowledge_base_catalog=True,
        )

    async def list_spaces(self):
        return [
            PolarRAGSpaceRecord(
                space_id="space-a",
                name="Space A",
                identity_domain="tenant-a",
                status="ACTIVE",
                oss_bucket=self.oss_bucket,
                oss_endpoint=self.oss_endpoint,
            )
        ]

    async def list_spaces_page(self, *, cursor, page_size):
        assert cursor is None
        return (await self.list_spaces())[:page_size], None

    async def list_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="public-kb",
                name="Public KB",
                kb_type="PUBLIC",
                identity_domain="tenant-a",
                owner=None,
            )
        ]

    async def list_knowledge_bases_page(
        self,
        space_id,
        *,
        cursor,
        page_size,
    ):
        assert cursor is None
        return (await self.list_knowledge_bases(space_id))[:page_size], None

    async def list_unclaimed_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        return []

    async def list_unclaimed_knowledge_bases_page(
        self,
        space_id,
        *,
        cursor,
        page_size,
    ):
        assert cursor is None
        return (await self.list_unclaimed_knowledge_bases(space_id))[:page_size], None


class PaginatedSpacesClient(FakeAdminClient):
    def __init__(self) -> None:
        super().__init__()
        self.cursors: list[str | None] = []

    async def list_spaces(self):
        raise AssertionError("unbounded Space listing must not be used")

    async def list_spaces_page(self, *, cursor, page_size):
        self.cursors.append(cursor)
        if cursor is None:
            return [
                PolarRAGSpaceRecord(
                    space_id="space-other",
                    name="Other",
                    identity_domain="tenant-a",
                    status="ACTIVE",
                )
            ], "page-2"
        assert cursor == "page-2"
        return [
            PolarRAGSpaceRecord(
                space_id="space-a",
                name="Space A",
                identity_domain="tenant-a",
                status="ACTIVE",
            )
        ], None


async def test_find_upstream_space_uses_bounded_pages() -> None:
    client = PaginatedSpacesClient()

    result = await _find_upstream_space(client, "space-a")

    assert result is not None
    assert result.space_id == "space-a"
    assert client.cursors == [None, "page-2"]


async def test_find_upstream_space_rejects_repeated_cursor() -> None:
    class RepeatedCursorClient(FakeAdminClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_spaces_page(self, *, cursor, page_size):
            self.calls += 1
            if self.calls > 2:
                raise AssertionError("repeated cursor was not rejected")
            return [], "same-cursor"

    client = RepeatedCursorClient()

    with pytest.raises(PolarRAGUpstreamError) as exc_info:
        await _find_upstream_space(client, "missing-space")

    assert exc_info.value.code == PolarRAGErrorCode.INVALID_RESPONSE
    assert client.calls == 2


async def test_find_upstream_space_limits_page_count(monkeypatch) -> None:
    class EndlessCursorClient(FakeAdminClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_spaces_page(self, *, cursor, page_size):
            self.calls += 1
            return [], f"cursor-{self.calls}"

    client = EndlessCursorClient()
    monkeypatch.setattr("server.api.polarrag._MAX_CATALOG_PAGES", 2)

    with pytest.raises(PolarRAGUpstreamError) as exc_info:
        await _find_upstream_space(client, "missing-space")

    assert exc_info.value.code == PolarRAGErrorCode.INVALID_RESPONSE
    assert client.calls == 2


async def test_find_knowledge_base_rejects_repeated_cursor() -> None:
    class RepeatedCursorClient(FakeAdminClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_unclaimed_knowledge_bases_page(
            self,
            space_id,
            *,
            cursor,
            page_size,
        ):
            self.calls += 1
            if self.calls > 2:
                raise AssertionError("repeated cursor was not rejected")
            return [
                PolarRAGKnowledgeBaseRecord(
                    space_id=space_id,
                    kb_id=f"other-{self.calls}",
                    name="Other",
                    kb_type="PERSONAL",
                    identity_domain="tenant-a",
                    owner=None,
                    status="UNCLAIMED",
                )
            ], "same-cursor"

    client = RepeatedCursorClient()

    with pytest.raises(PolarRAGUpstreamError) as exc_info:
        await _find_knowledge_base(
            client,
            "space-a",
            "missing-kb",
            status="UNCLAIMED",
        )

    assert exc_info.value.code == PolarRAGErrorCode.INVALID_RESPONSE
    assert client.calls == 2


async def test_find_knowledge_base_limits_page_count(monkeypatch) -> None:
    class EndlessCursorClient(FakeAdminClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_unclaimed_knowledge_bases_page(
            self,
            space_id,
            *,
            cursor,
            page_size,
        ):
            self.calls += 1
            return [
                PolarRAGKnowledgeBaseRecord(
                    space_id=space_id,
                    kb_id=f"other-{self.calls}",
                    name="Other",
                    kb_type="PERSONAL",
                    identity_domain="tenant-a",
                    owner=None,
                    status="UNCLAIMED",
                )
            ], f"cursor-{self.calls}"

    client = EndlessCursorClient()
    monkeypatch.setattr("server.api.polarrag._MAX_CATALOG_PAGES", 2)

    with pytest.raises(PolarRAGUpstreamError) as exc_info:
        await _find_knowledge_base(
            client,
            "space-a",
            "missing-kb",
            status="UNCLAIMED",
        )

    assert exc_info.value.code == PolarRAGErrorCode.INVALID_RESPONSE
    assert client.calls == 2

class MissingIdentityDomainClient(FakeAdminClient):
    async def list_spaces(self):
        return [
            PolarRAGSpaceRecord(
                space_id="legacy-space",
                name="Legacy Space",
                identity_domain=None,
                status="ACTIVE",
            )
        ]


class InactiveSpaceClient(FakeAdminClient):
    async def list_spaces(self):
        return [
            PolarRAGSpaceRecord(
                space_id="space-disabled",
                name="Disabled Space",
                identity_domain="tenant-a",
                status="DISABLED",
            )
        ]


class MutableAdminClient(FakeAdminClient):
    def __init__(self) -> None:
        super().__init__()
        self.spaces: list[PolarRAGSpaceRecord] = []

    async def list_spaces(self):
        return self.spaces


class FailingCatalogClient(FakeAdminClient):
    async def list_knowledge_bases(self, space_id):
        raise PolarRAGUpstreamError(
            PolarRAGErrorCode.UNAVAILABLE,
            retryable=True,
        )


class FakeClaimClient(FakeAdminClient):
    def __init__(self) -> None:
        super().__init__()
        self.claimed = False
        self.claim_owner = None

    async def list_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        if not self.claimed:
            return []
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="personal-kb",
                name="Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-a",
                owner={
                    "provider": "feishu",
                    "type": "user",
                    "id": "ou-owner",
                },
                status="ACTIVE",
            )
        ]

    async def list_unclaimed_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        if self.claimed:
            return []
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="personal-kb",
                name="Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-a",
                owner=None,
                status="UNCLAIMED",
            )
        ]

    async def claim_knowledge_base(self, space_id, kb_id, *, owner):
        assert (space_id, kb_id) == ("space-a", "personal-kb")
        self.claim_owner = owner
        self.claimed = True


class RetryableClaimClient(FakeClaimClient):
    def __init__(self) -> None:
        super().__init__()
        self.claim_calls = 0
        self.fail_sync_once = True

    async def list_knowledge_bases(self, space_id):
        if self.claimed and self.fail_sync_once:
            self.fail_sync_once = False
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.UNAVAILABLE,
                retryable=True,
            )
        if not self.claimed:
            return []
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="personal-kb",
                name="Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-a",
                owner={
                    "provider": "polarrag",
                    "type": "user",
                    "id": self.claim_owner,
                },
                status="ACTIVE",
            )
        ]

    async def claim_knowledge_base(self, space_id, kb_id, *, owner):
        self.claim_calls += 1
        await super().claim_knowledge_base(
            space_id,
            kb_id,
            owner=owner,
        )


class MismatchedCatalogClient(FakeAdminClient):
    async def list_unclaimed_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="foreign-personal-kb",
                name="Foreign Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-b",
                owner=None,
                status="UNCLAIMED",
            )
        ]


async def test_admin_registers_encrypted_instance_and_enables_trusted_space(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, _member = setup
    fake = FakeAdminClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    secret = "do-not-return"

    forbidden = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": secret,
            "tls_verify": True,
        },
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": secret,
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert secret not in created.text
    assert "username" not in created.json()
    instance_id = created.json()["id"]

    spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    assert spaces.status_code == 200
    discovered_space = spaces.json()["items"][0]
    assert discovered_space["identity_domain"] == "tenant-a"
    assert discovered_space["oss_bucket"] == "tenant-a-documents"
    assert discovered_space["oss_endpoint"] == "oss-cn-hangzhou.aliyuncs.com"
    assert discovered_space["enabled"] is True
    assert discovered_space["knowledge_space_id"] is not None
    enabled_space = discovered_space
    assert enabled_space["enabled"] is True
    assert enabled_space["last_synced_at"] is not None
    assert enabled_space["knowledge_resource_count"] == 1

    resources = await http.get(
        f"/api/polarrag/instances/{instance_id}/knowledge-resources",
        headers=admin_headers,
    )
    assert resources.status_code == 200
    assert resources.json()["total"] == 1
    assert resources.json()["items"] == [
        {
            "knowledge_resource_id": resources.json()["items"][0][
                "knowledge_resource_id"
            ],
            "name": "Public KB",
            "space_id": "space-a",
            "space_name": "Space A",
            "kb_type": "PUBLIC",
            "binding_mode": "domain",
            "sync_status": "active",
            "enabled": True,
            "management_mode": "NATIVE",
        }
    ]

    validation_calls: list[dict[str, str]] = []

    async def validate_oss(**kwargs) -> None:
        validation_calls.append(kwargs)

    monkeypatch.setattr(
        "server.api.polarrag.validate_oss_write_access",
        validate_oss,
    )
    oss_payload = {
        "access_key_id": "  test-access-key  ",
        "access_key_secret": "test-secret-key",
        "object_prefix": "pas/documents",
    }
    forbidden_oss = await http.put(
        f"/api/polarrag/spaces/{enabled_space['knowledge_space_id']}/oss-config",
        json=oss_payload,
        headers=member_headers,
    )
    assert forbidden_oss.status_code == 403

    configured_oss = await http.put(
        f"/api/polarrag/spaces/{enabled_space['knowledge_space_id']}/oss-config",
        json=oss_payload,
        headers=admin_headers,
    )
    assert configured_oss.status_code == 200
    assert configured_oss.json()["bucket"] == "tenant-a-documents"
    assert configured_oss.json()["endpoint"] == "oss-cn-hangzhou.aliyuncs.com"
    assert configured_oss.json()["validated"] is True
    assert "access_key" not in configured_oss.text
    assert "test-secret-key" not in configured_oss.text
    assert validation_calls == [
        {
            "endpoint": "oss-cn-hangzhou.aliyuncs.com",
            "bucket": "tenant-a-documents",
            "access_key_id": "test-access-key",
            "access_key_secret": "test-secret-key",
            "object_prefix": "pas/documents",
        }
    ]

    fake.oss_endpoint = "oss-cn-beijing.aliyuncs.com"
    refreshed_spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    assert refreshed_spaces.status_code == 200
    assert refreshed_spaces.json()["items"][0]["oss_bucket"] == "tenant-a-documents"
    assert refreshed_spaces.json()["items"][0]["oss_endpoint"] == (
        "oss-cn-beijing.aliyuncs.com"
    )

    disabled = await http.delete(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a",
        headers=admin_headers,
    )
    assert disabled.status_code == 204

    checked = await http.post(
        f"/api/polarrag/instances/{instance_id}/check",
        headers=admin_headers,
    )
    assert checked.status_code == 200

    async with factory() as session:
        instance = await session.get(PolarRAGInstance, instance_id)
        assert decrypt(instance.username_ciphertext) == "shared"
        assert decrypt(instance.password_ciphertext) == secret
        space = (
            await session.execute(select(PolarRAGSpace))
        ).scalar_one()
        resource = (
            await session.execute(select(KnowledgeResource))
        ).scalar_one()
        assert space.identity_domain == "tenant-a"
        assert space.oss_bucket == "tenant-a-documents"
        assert space.oss_endpoint == "oss-cn-beijing.aliyuncs.com"
        assert space.oss_config_validated is False
        assert space.oss_last_error_code == "OSS_CATALOG_CHANGED"
        assert decrypt(space.oss_access_key_id_ciphertext) == "test-access-key"
        assert decrypt(space.oss_access_key_secret_ciphertext) == "test-secret-key"
        assert resource.kb_id == "public-kb"
        assert space.enabled is False
        assert resource.enabled is False


@pytest.mark.parametrize("operation", ["check", "update"])
async def test_instance_mutation_activates_newly_discovered_spaces(
    client,
    setup,
    monkeypatch,
    operation,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    fake = MutableAdminClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "secret",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    fake.spaces = [
        PolarRAGSpaceRecord(
            space_id="space-a",
            name="Space A",
            identity_domain="tenant-a",
            status="ACTIVE",
        )
    ]

    if operation == "check":
        response = await http.post(
            f"/api/polarrag/instances/{instance_id}/check",
            headers=admin_headers,
        )
    else:
        response = await http.patch(
            f"/api/polarrag/instances/{instance_id}",
            json={"name": "Updated RAG"},
            headers=admin_headers,
        )

    assert response.status_code == 200
    async with factory() as session:
        space = (await session.execute(select(PolarRAGSpace))).scalar_one()
        resource = (
            await session.execute(select(KnowledgeResource))
        ).scalar_one()
        assert space.enabled is True
        assert resource.enabled is True


async def test_instance_create_rolls_back_when_automatic_space_sync_fails(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: FailingCatalogClient(),
    )

    failed = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Atomic RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "secret",
            "tls_verify": True,
        },
        headers=admin_headers,
    )

    assert failed.status_code == 503
    async with factory() as session:
        assert (
            await session.execute(select(PolarRAGInstance))
        ).scalar_one_or_none() is None
        assert (
            await session.execute(select(PolarRAGSpace))
        ).scalar_one_or_none() is None


async def test_admin_lists_unconfigured_space_but_cannot_enable_it(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: MissingIdentityDomainClient(),
    )

    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Legacy RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "secret",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    instance_id = created.json()["id"]

    spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    assert spaces.status_code == 200
    items = spaces.json()["items"]
    assert len(items) == 1
    assert items[0]["space_id"] == "legacy-space"
    assert items[0]["identity_domain"] is None
    assert items[0]["enabled"] is False

    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "legacy-space"},
        headers=admin_headers,
    )
    assert enabled.status_code == 409
    assert enabled.json()["detail"]["code"] == "POLARRAG_IDENTITY_DOMAIN_UNAVAILABLE"


async def test_space_sync_returns_202_and_continues_in_background(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    _factory, _admin, _member = setup
    fake_client = FakeAdminClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake_client,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Async RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "secret",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    assert enabled.status_code == 200

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sync(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {
            "knowledge_bases": 1,
            "active": 1,
            "disabled": 0,
            "owner_unresolved": 0,
        }

    monkeypatch.setattr("server.polarrag.catalog.sync_space_catalog", slow_sync)
    response = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/sync",
        headers=admin_headers,
    )
    assert response.status_code == 202
    assert response.json() == {
        "status": "running",
        "result": None,
        "error": None,
    }
    await started.wait()

    status = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/sync",
        headers=admin_headers,
    )
    assert status.json()["status"] == "running"
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        status = await http.get(
            f"/api/polarrag/instances/{instance_id}/spaces/space-a/sync",
            headers=admin_headers,
        )
        if status.json()["status"] == "completed":
            break
    assert status.json()["result"]["knowledge_bases"] == 1


async def test_space_sync_state_and_claim_are_shared_across_app_instances(
    client, setup, monkeypatch
) -> None:
    http_one, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        instance = PolarRAGInstance(
            name="Shared state RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext=encrypt("username"),
            password_ciphertext=encrypt("password"),
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="shared-space",
            name="Shared Space",
            identity_domain="tenant-shared",
            enabled=True,
        )
        session.add(space)
        await session.commit()
        instance_id = instance.id

    calls = 0

    async def fast_sync(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"knowledge_bases": 0, "active": 0, "disabled": 0, "owner_unresolved": 0}

    async with factory() as session:
        stored = await session.scalar(select(PolarRAGSpace))
        assert stored is not None
        stored.catalog_sync_status = "running"
        stored.catalog_sync_worker_id = "first-process"
        stored.catalog_sync_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

    monkeypatch.setattr("server.polarrag.catalog.sync_space_catalog", fast_sync)
    from tests._knowledge_helpers import enable_knowledge_routes

    app_two = enable_knowledge_routes(create_app())
    async with AsyncClient(
        transport=ASGITransport(app=app_two), base_url="http://second-app"
    ) as http_two:
        first = await http_one.get(
            f"/api/polarrag/instances/{instance_id}/spaces/shared-space/sync",
            headers=admin_headers,
        )
        assert first.json()["status"] == "running"

        duplicate = await http_two.post(
            f"/api/polarrag/instances/{instance_id}/spaces/shared-space/sync",
            headers=admin_headers,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "running"
        observed = await http_two.get(
            f"/api/polarrag/instances/{instance_id}/spaces/shared-space/sync",
            headers=admin_headers,
        )
        assert observed.json()["status"] == "running"

        async with factory() as session:
            stored = await session.scalar(select(PolarRAGSpace))
            assert stored is not None
            stored.catalog_sync_lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        restarted = await http_two.post(
            f"/api/polarrag/instances/{instance_id}/spaces/shared-space/sync",
            headers=admin_headers,
        )
        assert restarted.status_code == 202
        for _ in range(20):
            await asyncio.sleep(0)
            observed = await http_two.get(
                f"/api/polarrag/instances/{instance_id}/spaces/shared-space/sync",
                headers=admin_headers,
            )
            if observed.json()["status"] == "completed":
                break
        assert observed.json()["status"] == "completed", observed.json()
    assert calls == 1


@pytest.mark.parametrize("stale_status_code", [None, 409])
async def test_reclaimed_space_sync_rolls_back_stale_worker_catalog_writes(
    setup,
    monkeypatch,
    stale_status_code,
) -> None:
    factory, admin, _member = setup
    async with factory() as session:
        instance = PolarRAGInstance(
            name="Fenced sync RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext=encrypt("username"),
            password_ciphertext=encrypt("password"),
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="fenced-space",
            name="Fenced Space",
            identity_domain="tenant-fenced",
            enabled=True,
            catalog_sync_status="running",
            catalog_sync_worker_id="old-worker",
            catalog_sync_lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=instance.id,
            space_id=space.space_id,
            kb_id="kb-a",
            name="initial",
            kb_type="PUBLIC",
            identity_domain=space.identity_domain,
            sync_status="active",
            enabled=True,
            catalog_sync_token="initial-token",
        )
        session.add(resource)
        await session.commit()
        space_id = space.knowledge_space_id
        resource_id = resource.id

    old_started = asyncio.Event()
    release_old = asyncio.Event()

    async def overlapping_sync(session, synced_space, client, *, commit):
        assert commit is False
        if client == "old":
            old_started.set()
            await release_old.wait()
        resource = await session.get(KnowledgeResource, resource_id)
        assert resource is not None
        resource.name = f"{client}-result"
        resource.catalog_sync_token = f"{client}-token"
        if client == "old" and stale_status_code is not None:
            synced_space.enabled = False
            await session.flush()
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.UNAVAILABLE,
                status_code=stale_status_code,
            )
        return {
            "knowledge_bases": 1,
            "active": 1 if client == "old" else 2,
            "disabled": 0,
            "owner_unresolved": 0,
        }

    monkeypatch.setattr("server.polarrag.catalog.sync_space_catalog", overlapping_sync)
    old_task = asyncio.create_task(
        _run_claimed_space_catalog_sync(
            factory,
            space_id,
            "old-worker",
            client_factory=lambda _instance: "old",
        )
    )
    await old_started.wait()

    async with factory() as session:
        stored = await session.get(PolarRAGSpace, space_id)
        assert stored is not None
        stored.catalog_sync_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with factory() as session:
        assert await claim_space_catalog_sync(session, space_id, "new-worker")
    assert await _run_claimed_space_catalog_sync(
        factory,
        space_id,
        "new-worker",
        client_factory=lambda _instance: "new",
    ) == {
        "knowledge_bases": 1,
        "active": 2,
        "disabled": 0,
        "owner_unresolved": 0,
    }

    release_old.set()
    assert await old_task is None

    async with factory() as session:
        stored_space = await session.get(PolarRAGSpace, space_id)
        stored_resource = await session.get(KnowledgeResource, resource_id)
        assert stored_space is not None
        assert stored_resource is not None
        assert stored_space.catalog_sync_status == "completed"
        assert stored_space.enabled is True
        assert stored_space.catalog_sync_result_json is not None
        assert '"active": 2' in stored_space.catalog_sync_result_json
        assert stored_resource.name == "new-result"
        assert stored_resource.catalog_sync_token == "new-token"


async def test_instance_registration_does_not_auto_enable_inactive_space(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: InactiveSpaceClient(),
    )

    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG with disabled Space",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "secret",
            "tls_verify": True,
        },
        headers=admin_headers,
    )

    assert created.status_code == 201
    async with factory() as session:
        assert (
            await session.execute(select(PolarRAGSpace))
        ).scalar_one_or_none() is None


async def test_instance_create_rolls_back_when_required_audit_fails(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: FakeAdminClient(),
    )

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr("server.api.polarrag.log_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit storage unavailable"):
        await http.post(
            "/api/polarrag/instances",
            json={
                "name": "Atomic RAG",
                "scheme": "https",
                "host": "rag.example.test",
                "port": 9200,
                "username": "shared",
                "password": "not-returned",
                "tls_verify": True,
            },
            headers=admin_headers,
        )

    async with factory() as session:
        assert (
            await session.execute(select(PolarRAGInstance))
        ).scalar_one_or_none() is None


async def test_principal_api_enforces_provider_and_user_uniqueness(
    client,
    setup,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, member = setup
    invalid = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "caller-controlled",
            "principal_type": "user",
            "principal_id": "id-1",
        },
        headers=admin_headers,
    )
    assert invalid.status_code == 422

    native_spoof = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "user",
            "principal_id": member.id,
        },
        headers=admin_headers,
    )
    assert native_spoof.status_code == 422

    native_group = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "group",
            "principal_id": member.external_id,
        },
        headers=admin_headers,
    )
    assert native_group.status_code == 422

    native = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "user",
            "principal_id": member.external_id,
        },
        headers=admin_headers,
    )
    assert native.status_code == 201
    assert native.json()["principal_id"] == member.external_id

    blank = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": " ",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": " ",
        },
        headers=admin_headers,
    )
    assert blank.status_code == 422

    created = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-1",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201

    second_user = await http.post(
        "/api/users",
        json={
            "username": "second",
            "password": "strong-password",
        },
        headers=admin_headers,
    )
    assert second_user.status_code == 201
    conflict = await http.post(
        f"/api/polarrag/users/{second_user.json()['id']}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-1",
        },
        headers=admin_headers,
    )
    assert conflict.status_code == 409

    async with factory() as session:
        assignments = (
            await session.execute(select(EnterprisePrincipalAssignment))
        ).scalars().all()
        assert len(assignments) == 2


@pytest.mark.parametrize(
    ("auth_provider", "external_id"),
    [
        ("builtin", "builtin-user "),
        ("oidc", "oidc:user "),
    ],
)
async def test_principal_api_preserves_native_user_external_id_whitespace(
    client,
    setup,
    auth_provider,
    external_id,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, member = setup
    async with factory() as session:
        stored_member = await session.get(User, member.id)
        assert stored_member is not None
        stored_member.external_id = external_id
        if auth_provider == "oidc":
            stored_member.auth_provider = AuthProvider.OIDC
        await session.commit()

    response = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "user",
            "principal_id": external_id,
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["principal_id"] == external_id


async def test_admin_assigns_unclaimed_kb_owner_and_synchronizes_catalog(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    factory, admin, member = setup
    fake = FakeClaimClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "not-returned",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    assert enabled.status_code == 200
    principal = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-owner",
        },
        headers=admin_headers,
    )
    principal_id = principal.json()["id"]
    async with factory() as session:
        spoof = EnterprisePrincipalAssignment(
            pas_user_id=member.id,
            identity_domain="tenant-a",
            provider="polarrag",
            principal_type=EnterprisePrincipalType.USER,
            principal_id="another-user",
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
            status=EnterprisePrincipalStatus.ACTIVE,
            user_principal_key="spoofed-native-owner",
        )
        session.add(spoof)
        await session.commit()
        spoof_id = spoof.id

    forbidden = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    wrong_domain = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-b",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-wrong-domain",
        },
        headers=admin_headers,
    )
    assert wrong_domain.status_code == 201
    spoofed_owner = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": spoof_id},
        headers=admin_headers,
    )
    assert spoofed_owner.status_code == 422
    rejected = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": wrong_domain.json()["id"]},
        headers=admin_headers,
    )
    assert rejected.status_code == 422
    assert fake.claim_owner is None

    pending = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=admin_headers,
    )
    assert pending.status_code == 200
    assert pending.json()["items"] == [
        {
            "space_id": "space-a",
            "space_name": "Space A",
            "identity_domain": "tenant-a",
            "kb_id": "personal-kb",
            "name": "Personal KB",
            "kb_type": "PERSONAL",
            "status": "UNCLAIMED",
        }
    ]
    candidates = await http.get(
        f"/api/polarrag/instances/{instance_id}/owner-candidates",
        params={"identity_domain": "tenant-a"},
        headers=admin_headers,
    )
    assert candidates.status_code == 200
    assert {
        "principal_assignment_id": principal_id,
        "pas_user_id": member.id,
        "user_name": "Member",
        "user_external_id": member.external_id,
        "identity_domain": "tenant-a",
        "provider": "feishu",
        "principal_id": "ou-owner",
    } in candidates.json()["items"]

    claimed = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": principal_id},
        headers=admin_headers,
    )
    assert claimed.status_code == 200
    assert claimed.json() == {
        "kb_id": "personal-kb",
        "status": "ACTIVE",
        "sync": {
            "knowledge_bases": 1,
            "active": 1,
            "disabled": 0,
            "owner_unresolved": 0,
        },
    }
    assert fake.claim_owner == member.external_id
    async with factory() as session:
        resource = (await session.execute(select(KnowledgeResource))).scalar_one()
        assert resource.kb_id == "personal-kb"
        assert resource.enabled is True
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "polarrag.knowledge_base.claim"
        )
            )
        ).scalar_one()
        assert audit.actor_user_id == admin.id
        assert audit.target_id == "space-a/personal-kb"


async def test_admin_assigns_unclaimed_kb_to_active_native_pas_user(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    _factory, _admin, member = setup
    fake = FakeClaimClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG native owner",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "not-returned",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    assert enabled.status_code == 200

    pending = await http.get(
        f"/api/polarrag/instances/{instance_id}/owner-candidates",
        params={"identity_domain": "tenant-a"},
        headers=admin_headers,
    )

    assert pending.status_code == 200
    native_owner = next(
        candidate
        for candidate in pending.json()["items"]
        if candidate["pas_user_id"] == member.id
        and candidate["provider"] == "polarrag"
        and candidate["identity_domain"] == "tenant-a"
    )
    assert native_owner["principal_assignment_id"] is None
    claimed = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"pas_user_id": member.id},
        headers=admin_headers,
    )

    assert claimed.status_code == 200
    assert fake.claim_owner == member.external_id


async def test_owner_candidates_are_domain_scoped_and_sql_paginated(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        instance = PolarRAGInstance(
            name="Candidate RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext="username",
            password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        session.add(
            PolarRAGSpace(
                polarrag_instance_id=instance.id,
                space_id="space-candidates",
                name="Candidates",
                identity_domain="tenant-candidates",
                enabled=True,
            )
        )
        session.add_all(
            [
                User(
                    external_id=f"candidate-{index:03d}",
                    display_name=f"Candidate {index:03d}",
                    auth_provider=AuthProvider.BUILTIN,
                )
                for index in range(30)
            ]
        )
        await session.commit()
        instance_id = instance.id

    statements: list[tuple[str, object]] = []

    def record_statement(_conn, _cursor, statement, parameters, _context, _many):
        if "UNION ALL" in statement and " LIMIT " in statement:
            statements.append((statement, parameters))

    event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", record_statement)
    try:
        response = await http.get(
            f"/api/polarrag/instances/{instance_id}/owner-candidates",
            params={"identity_domain": "tenant-candidates", "limit": 5},
            headers=admin_headers,
        )
    finally:
        event.remove(factory.kw["bind"].sync_engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert response.json()["total"] == 32
    assert len(response.json()["items"]) == 5
    assert len(statements) == 1
    assert 5 in statements[0][1]


async def test_claim_retry_recovers_after_upstream_success_and_sync_failure(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, member = setup
    fake = RetryableClaimClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    instance = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Retry RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "not-returned",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = instance.json()["id"]
    await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    principal = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-owner",
        },
        headers=admin_headers,
    )
    endpoint = (
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/"
        "knowledge-bases/personal-kb/claim"
    )

    first = await http.post(
        endpoint,
        json={"principal_assignment_id": principal.json()["id"]},
        headers=admin_headers,
    )
    second = await http.post(
        endpoint,
        json={"principal_assignment_id": principal.json()["id"]},
        headers=admin_headers,
    )

    assert first.status_code == 503
    assert second.status_code == 200
    assert fake.claim_calls == 1
    async with factory() as session:
        resource = (
            await session.execute(
                select(KnowledgeResource).where(
                    KnowledgeResource.kb_id == "personal-kb"
                )
            )
        ).scalar_one()
        assert resource.owner_pas_user_id == member.id


async def test_unclaimed_catalog_rejects_identity_boundary_as_upstream_error(
    client,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    fake = MismatchedCatalogClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Boundary RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "not-returned",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    assert enabled.status_code == 200

    response = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "POLARRAG_INVALID_RESPONSE",
            "message": "POLARRAG_INVALID_RESPONSE",
        }
    }
