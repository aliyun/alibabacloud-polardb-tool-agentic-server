from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.models import (
    AuthProvider,
    Base,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
)
from server.polarrag.catalog import (
    sync_enabled_spaces_once,
    sync_space_catalog,
)
from server.polarrag.contracts import (
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGUpstreamError,
)


class FakeCatalogClient:
    def __init__(self, records: list[PolarRAGKnowledgeBaseRecord]):
        self.records = records

    async def list_knowledge_bases(
        self,
        space_id: str,
    ) -> list[PolarRAGKnowledgeBaseRecord]:
        assert space_id == "space-a"
        return self.records


class FailingCatalogClient:
    def __init__(self, status_code: int, *, retryable: bool = False):
        self.error = PolarRAGUpstreamError(
            PolarRAGErrorCode.INVALID_RESPONSE,
            status_code=status_code,
            retryable=retryable,
        )

    async def list_knowledge_bases(self, space_id: str):
        assert space_id == "space-a"
        raise self.error


async def _add_active_resource(session, space: PolarRAGSpace) -> None:
    session.add(
        KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=space.polarrag_instance_id,
            space_id=space.space_id,
            kb_id="public-kb",
            name="Public",
            kb_type="PUBLIC",
            identity_domain=space.identity_domain,
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            enabled=True,
        )
    )
    await session.commit()


@pytest.fixture
async def seeded():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
        )
        owner = User(
            external_id="owner",
            display_name="Owner",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add_all([admin, owner])
        await session.flush()
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=owner.id,
                identity_domain="tenant-a",
                provider="feishu",
                principal_type=EnterprisePrincipalType.USER,
                principal_id="ou-owner",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
        instance = PolarRAGInstance(
            name="RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext="u",
            password_ciphertext="p",
            status=PolarRAGInstanceStatus.ACTIVE,
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
        yield session, space, owner
    await engine.dispose()


async def test_sync_space_catalog_applies_domain_owner_and_unknown_rules(
    seeded,
) -> None:
    session, space, owner = seeded
    records = [
        PolarRAGKnowledgeBaseRecord(
            space_id="space-a",
            kb_id="public-kb",
            name="Public",
            kb_type="PUBLIC",
            identity_domain="tenant-a",
            owner=None,
        ),
        PolarRAGKnowledgeBaseRecord(
            space_id="space-a",
            kb_id="personal-kb",
            name="Personal",
            kb_type="PERSONAL",
            identity_domain="tenant-a",
            owner={"provider": "feishu", "type": "user", "id": "ou-owner"},
        ),
        PolarRAGKnowledgeBaseRecord(
            space_id="space-a",
            kb_id="future-kb",
            name="Future",
            kb_type="TEAM",
            identity_domain="tenant-a",
            owner=None,
        ),
    ]

    result = await sync_space_catalog(
        session,
        space,
        FakeCatalogClient(records),
        now=datetime.now(UTC),
    )
    resources = {
        row.kb_id: row
        for row in (
            await session.execute(select(KnowledgeResource))
        ).scalars()
    }

    assert result == {"active": 2, "disabled": 1, "owner_unresolved": 0}
    assert resources["public-kb"].binding_mode == KnowledgeBindingMode.DOMAIN
    assert resources["personal-kb"].binding_mode == KnowledgeBindingMode.OWNER
    assert resources["personal-kb"].owner_pas_user_id == owner.id
    assert resources["future-kb"].binding_mode is None
    assert (
        resources["future-kb"].sync_status
        == KnowledgeResourceSyncStatus.UNSUPPORTED_KB_TYPE
    )
    assert resources["future-kb"].enabled is False


async def test_sync_space_catalog_hides_unresolved_personal_owner(
    seeded,
) -> None:
    session, space, _owner = seeded
    record = PolarRAGKnowledgeBaseRecord(
        space_id="space-a",
        kb_id="personal-kb",
        name="Personal",
        kb_type="PERSONAL",
        identity_domain="tenant-a",
        owner={"provider": "sharepoint", "type": "user", "id": "missing"},
    )

    result = await sync_space_catalog(
        session,
        space,
        FakeCatalogClient([record]),
    )
    resource = (
        await session.execute(select(KnowledgeResource))
    ).scalar_one()

    assert result["owner_unresolved"] == 1
    assert resource.sync_status == KnowledgeResourceSyncStatus.OWNER_UNRESOLVED
    assert resource.enabled is False
    assert resource.owner_pas_user_id is None


async def test_periodic_sync_worker_visits_enabled_spaces(seeded) -> None:
    session, space, _owner = seeded
    factory = async_sessionmaker(
        session.bind,
        expire_on_commit=False,
    )
    calls: list[str] = []

    class WorkerClient(FakeCatalogClient):
        async def list_knowledge_bases(self, space_id):
            calls.append(space_id)
            return []

    await sync_enabled_spaces_once(
        factory,
        client_factory=lambda _instance: WorkerClient([]),
    )

    assert calls == [space.space_id]


async def test_periodic_sync_disables_catalog_ineligible_space_and_resources(
    seeded,
) -> None:
    session, space, _owner = seeded
    await _add_active_resource(session, space)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    await sync_enabled_spaces_once(
        factory,
        client_factory=lambda _instance: FailingCatalogClient(409),
    )

    async with factory() as verification_session:
        stored_space = (
            await verification_session.execute(select(PolarRAGSpace))
        ).scalar_one()
        resource = (
            await verification_session.execute(select(KnowledgeResource))
        ).scalar_one()
        assert stored_space.enabled is False
        assert resource.enabled is False
        assert (
            resource.sync_status
            == KnowledgeResourceSyncStatus.UPSTREAM_DISABLED
        )


async def test_periodic_sync_keeps_catalog_enabled_on_transient_failure(
    seeded,
) -> None:
    session, space, _owner = seeded
    await _add_active_resource(session, space)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    await sync_enabled_spaces_once(
        factory,
        client_factory=lambda _instance: FailingCatalogClient(
            503,
            retryable=True,
        ),
    )

    async with factory() as verification_session:
        stored_space = (
            await verification_session.execute(select(PolarRAGSpace))
        ).scalar_one()
        resource = (
            await verification_session.execute(select(KnowledgeResource))
        ).scalar_one()
        assert stored_space.enabled is True
        assert resource.enabled is True
        assert resource.sync_status == KnowledgeResourceSyncStatus.ACTIVE
