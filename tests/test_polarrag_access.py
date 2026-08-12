from __future__ import annotations

import pytest
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
from server.polarrag.access import (
    KnowledgeAccessError,
    KnowledgeAccessErrorCode,
    plan_knowledge_access,
)


@pytest.fixture
async def seeded():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(external_id="admin", display_name="Admin", auth_provider=AuthProvider.BUILTIN)
        user = User(external_id="user", display_name="User", auth_provider=AuthProvider.BUILTIN)
        other = User(external_id="other", display_name="Other", auth_provider=AuthProvider.BUILTIN)
        session.add_all([admin, user, other])
        await session.flush()
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=user.id,
                identity_domain="tenant-a",
                provider="feishu",
                principal_type=EnterprisePrincipalType.USER,
                principal_id="ou-user",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
        instances = []
        spaces = []
        for suffix in ("one", "two"):
            instance = PolarRAGInstance(
                name=suffix,
                scheme="https",
                host=f"{suffix}.example.test",
                port=9200,
                username_ciphertext="u",
                password_ciphertext="p",
                status=PolarRAGInstanceStatus.ACTIVE,
                created_by=admin.id,
            )
            session.add(instance)
            await session.flush()
            instances.append(instance)
            space = PolarRAGSpace(
                polarrag_instance_id=instance.id,
                space_id=f"space-{suffix}",
                name=f"Space {suffix}",
                identity_domain="tenant-a",
                enabled=True,
            )
            session.add(space)
            await session.flush()
            spaces.append(space)
        resources = []
        for index, (instance, space) in enumerate(zip(instances, spaces), 1):
            resource = KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=instance.id,
                space_id=space.space_id,
                kb_id=f"kb-{index}",
                name=f"KB {index}",
                kb_type="PUBLIC",
                identity_domain="tenant-a",
                binding_mode=KnowledgeBindingMode.DOMAIN,
                sync_status=KnowledgeResourceSyncStatus.ACTIVE,
                enabled=True,
            )
            session.add(resource)
            resources.append(resource)
        owner_resource = KnowledgeResource(
            knowledge_space_id=spaces[0].knowledge_space_id,
            polarrag_instance_id=instances[0].id,
            space_id=spaces[0].space_id,
            kb_id="owner-only",
            name="Owner",
            kb_type="PERSONAL",
            identity_domain="tenant-a",
            binding_mode=KnowledgeBindingMode.OWNER,
            owner_pas_user_id=other.id,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            enabled=True,
        )
        session.add(owner_resource)
        await session.commit()
        yield session, user, resources, owner_resource
    await engine.dispose()


async def test_access_plan_keeps_inaccessible_resources_as_partial_failures(
    seeded,
) -> None:
    session, user, resources, owner_resource = seeded

    plan = await plan_knowledge_access(
        session,
        user,
        [
            resources[0].id,
            owner_resource.id,
            "00000000-0000-0000-0000-000000000000",
        ],
    )

    assert [resource.id for resource in plan.resources] == [resources[0].id]
    assert plan.acl_context["identity_domain"] == "tenant-a"
    assert plan.partial_failures == [
        {
            "knowledge_resource_id": owner_resource.id,
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        },
        {
            "knowledge_resource_id": "00000000-0000-0000-0000-000000000000",
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        },
    ]


async def test_access_plan_rejects_mixed_instances_after_visibility_filter(
    seeded,
) -> None:
    session, user, resources, _owner_resource = seeded

    with pytest.raises(KnowledgeAccessError) as captured:
        await plan_knowledge_access(
            session,
            user,
            [resources[0].id, resources[1].id],
        )

    assert captured.value.code == KnowledgeAccessErrorCode.MIXED_INSTANCE


async def test_agent_instance_scope_filters_resources(seeded) -> None:
    session, user, resources, _owner_resource = seeded

    plan = await plan_knowledge_access(
        session,
        user,
        [resources[0].id, resources[1].id],
        allowed_instance_ids={resources[0].polarrag_instance_id},
    )

    assert [resource.id for resource in plan.resources] == [resources[0].id]
    assert plan.partial_failures == [
        {
            "knowledge_resource_id": resources[1].id,
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        }
    ]


async def test_empty_agent_instance_scope_denies_all(seeded) -> None:
    session, user, resources, _owner_resource = seeded

    with pytest.raises(KnowledgeAccessError) as captured:
        await plan_knowledge_access(
            session,
            user,
            [resources[0].id],
            allowed_instance_ids=set(),
        )

    assert captured.value.code == KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE
