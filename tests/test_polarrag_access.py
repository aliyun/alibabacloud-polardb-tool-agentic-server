from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.models import (
    AuthProvider,
    Base,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
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
    KnowledgeResourceScope,
    list_visible_knowledge_resources,
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
        resource_scope=KnowledgeResourceScope(
            {resources[0].polarrag_instance_id: None}
        ),
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
            resource_scope=KnowledgeResourceScope({}),
        )

    assert captured.value.code == KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE


async def test_agent_public_scope_only_filters_public_resources(seeded) -> None:
    session, user, resources, _owner_resource = seeded
    instance_id = resources[0].polarrag_instance_id
    space = resources[0].space
    unlisted = KnowledgeResource(
        knowledge_space_id=space.knowledge_space_id,
        polarrag_instance_id=instance_id,
        space_id=space.space_id,
        kb_id="public-unlisted",
        name="Public unlisted",
        kb_type="PUBLIC",
        identity_domain=space.identity_domain,
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    personal = KnowledgeResource(
        knowledge_space_id=space.knowledge_space_id,
        polarrag_instance_id=instance_id,
        space_id=space.space_id,
        kb_id="personal-owned",
        name="Personal owned",
        kb_type="PERSONAL",
        identity_domain=space.identity_domain,
        binding_mode=KnowledgeBindingMode.OWNER,
        owner_pas_user_id=user.id,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add_all([unlisted, personal])
    await session.commit()

    plan = await plan_knowledge_access(
        session,
        user,
        [resources[0].id, unlisted.id, personal.id],
        resource_scope=KnowledgeResourceScope(
            {instance_id: {resources[0].id}}
        ),
    )

    assert [resource.id for resource in plan.resources] == [
        resources[0].id,
        personal.id,
    ]
    assert plan.partial_failures == [
        {
            "knowledge_resource_id": unlisted.id,
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        }
    ]


async def test_agent_public_scope_cannot_expand_user_acl(seeded) -> None:
    session, user, resources, _owner_resource = seeded
    instance_id = resources[0].polarrag_instance_id
    blocked_space = PolarRAGSpace(
        polarrag_instance_id=instance_id,
        space_id="scope-blocked",
        name="Scope blocked",
        identity_domain="tenant-without-principal",
        enabled=True,
    )
    session.add(blocked_space)
    await session.flush()
    blocked = KnowledgeResource(
        knowledge_space_id=blocked_space.knowledge_space_id,
        polarrag_instance_id=instance_id,
        space_id=blocked_space.space_id,
        kb_id="scope-blocked",
        name="Scope blocked",
        kb_type="PUBLIC",
        identity_domain=blocked_space.identity_domain,
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add(blocked)
    await session.commit()

    with pytest.raises(KnowledgeAccessError) as captured:
        await plan_knowledge_access(
            session,
            user,
            [blocked.id],
            resource_scope=KnowledgeResourceScope(
                {instance_id: {blocked.id}}
            ),
        )

    assert captured.value.code == KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE


async def test_spoofed_native_principal_cannot_enable_discovery(seeded) -> None:
    session, user, resources, owner_resource = seeded
    owner_resource.owner_pas_user_id = user.id
    session.add(
        EnterprisePrincipalAssignment(
            pas_user_id=user.id,
            identity_domain=resources[0].identity_domain,
            provider="polarrag",
            principal_type=EnterprisePrincipalType.USER,
            principal_id="another-user",
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
            status=EnterprisePrincipalStatus.ACTIVE,
            user_principal_key="spoofed-native-user",
        )
    )
    await session.commit()

    assert await list_visible_knowledge_resources(session, user) == []
    with pytest.raises(KnowledgeAccessError) as captured:
        await plan_knowledge_access(session, user, [owner_resource.id])
    assert captured.value.code == KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE


async def test_native_personal_owner_is_visible_without_domain_principal(
    seeded,
) -> None:
    session, user, resources, owner_resource = seeded
    owner_resource.owner_pas_user_id = user.id
    await session.execute(
        delete(EnterprisePrincipalAssignment).where(
            EnterprisePrincipalAssignment.pas_user_id == user.id
        )
    )
    await session.commit()

    visible = await list_visible_knowledge_resources(session, user)
    assert {resource.id for resource in visible} == {
        *(resource.id for resource in resources),
        owner_resource.id,
    }
    plan = await plan_knowledge_access(session, user, [owner_resource.id])
    assert [resource.id for resource in plan.resources] == [owner_resource.id]
    assert plan.acl_context == {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "polarrag", "type": "user", "id": user.external_id}
        ],
    }


async def test_agent_instance_scope_includes_future_enabled_spaces_with_acl(
    seeded,
) -> None:
    session, user, resources, _owner_resource = seeded
    instance_id = resources[0].polarrag_instance_id
    future_space = PolarRAGSpace(
        polarrag_instance_id=instance_id,
        space_id="space-future",
        name="Future Space",
        identity_domain="tenant-a",
        enabled=True,
    )
    blocked_space = PolarRAGSpace(
        polarrag_instance_id=instance_id,
        space_id="space-future-blocked",
        name="Future Space Without User Principal",
        identity_domain="tenant-b",
        enabled=True,
    )
    session.add_all([future_space, blocked_space])
    await session.flush()
    future_resource = KnowledgeResource(
        knowledge_space_id=future_space.knowledge_space_id,
        polarrag_instance_id=instance_id,
        space_id=future_space.space_id,
        kb_id="kb-future",
        name="Future KB",
        kb_type="PUBLIC",
        identity_domain="tenant-a",
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    blocked_resource = KnowledgeResource(
        knowledge_space_id=blocked_space.knowledge_space_id,
        polarrag_instance_id=instance_id,
        space_id=blocked_space.space_id,
        kb_id="kb-future-blocked",
        name="Future KB Without User Principal",
        kb_type="PUBLIC",
        identity_domain="tenant-b",
        binding_mode=KnowledgeBindingMode.DOMAIN,
        sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        enabled=True,
    )
    session.add_all([future_resource, blocked_resource])
    await session.commit()

    plan = await plan_knowledge_access(
        session,
        user,
        [future_resource.id, blocked_resource.id],
        resource_scope=KnowledgeResourceScope({instance_id: None}),
    )

    assert [resource.id for resource in plan.resources] == [
        future_resource.id
    ]
    assert plan.acl_context["identity_domain"] == "tenant-a"
    assert plan.partial_failures == [
        {
            "knowledge_resource_id": blocked_resource.id,
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        }
    ]

    future_space.enabled = False
    await session.commit()
    with pytest.raises(KnowledgeAccessError) as captured:
        await plan_knowledge_access(
            session,
            user,
            [future_resource.id],
            resource_scope=KnowledgeResourceScope({instance_id: None}),
        )
    assert captured.value.code == KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE
