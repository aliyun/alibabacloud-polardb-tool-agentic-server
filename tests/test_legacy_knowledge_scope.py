from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope
from server.enterprise_identity.service import identity_provider_key
from server.models import (
    Agent, AgentKnowledgePolicy, AgentKnowledgeScope, AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode, AgentPolarRAGInstanceBinding, EnterpriseDirectoryUser,
    EnterpriseIdentitySource, EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus, EnterprisePrincipalAssignment,
    IdentitySourceProvider, KnowledgeBindingMode, KnowledgeResource,
    KnowledgeResourceSyncStatus, PolarRAGInstance, PolarRAGInstanceStatus,
    PolarRAGSpace, UserExternalIdentity,
)
from server.polarrag.access import KnowledgeAccessError, plan_knowledge_access

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture
async def legacy_resources(setup):
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="Existing local Agent", created_by=admin.id)
        instance = PolarRAGInstance(
            name="Existing RAG", scheme="http", host="rag.example.test", port=9200,
            username_ciphertext="unused", password_ciphertext="unused",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        session.add_all([agent, instance])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id, space_id="existing-space",
            name="Existing Space", identity_domain="existing-domain", enabled=True,
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id=space.space_id, kb_id="existing-kb", name="Existing KB",
            kb_type="PUBLIC", identity_domain=space.identity_domain,
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
        )
        binding = AgentPolarRAGInstanceBinding(
            agent_id=agent.id, polarrag_instance_id=instance.id, created_by_user_id=admin.id,
        )
        session.add_all([resource, binding])
        await session.commit()
        yield session, member, agent, resource, binding


@pytest.mark.parametrize("with_source", [False, True])
@pytest.mark.parametrize("mode", [AgentKnowledgeScopeMode.LEGACY_ALL, AgentKnowledgeScopeMode.SCOPED])
async def test_existing_local_access_does_not_require_enterprise_source(
    legacy_resources, with_source, mode
):
    session, member, agent, resource, binding = legacy_resources
    agent.knowledge_scope_mode = mode
    if with_source:
        source = EnterpriseIdentitySource.create(
            name="Unrelated source", provider=IdentitySourceProvider.FEISHU,
            tenant_id="unrelated-tenant",
        )
        session.add(source)
        await session.flush()
        session.add(EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id, knowledge_space_id=resource.knowledge_space_id,
        ))
    if mode == AgentKnowledgeScopeMode.SCOPED:
        grant = AgentKnowledgeScope(
            agent_id=agent.id, name="Existing direct grant", origin="MANUAL",
            knowledge_resource_ids_json=json.dumps([resource.id]),
        )
        session.add(grant)
        await session.flush()
        session.add(AgentKnowledgeScopeBinding(scope_id=grant.id, user_id=member.id))
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    plan = await plan_knowledge_access(session, member, [resource.id], resource_scope=scope)
    assert [row.id for row in plan.resources] == [resource.id]
    assert {"provider": "polarrag", "type": "user", "id": member.external_id} in plan.acl_context["principals"]

    binding.public_excluded_resource_ids_json = json.dumps([resource.id])
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert not scope.allows(resource)
    binding.public_excluded_resource_ids_json = "[]"
    await session.delete(binding)
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert not scope.allows(resource)


async def test_independent_principal_without_enterprise_source(legacy_resources):
    session, member, agent, resource, _ = legacy_resources
    session.add(EnterprisePrincipalAssignment(
        pas_user_id=member.id, identity_domain=resource.identity_domain,
        provider="feishu", principal_type="user", principal_id="legacy-principal",
        source="admin_managed", status="active",
    ))
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    plan = await plan_knowledge_access(session, member, [resource.id], resource_scope=scope)
    assert {"provider": "feishu", "type": "user", "id": "legacy-principal"} in plan.acl_context["principals"]


async def test_legacy_subject_exclusion_without_enterprise_source(legacy_resources):
    session, member, agent, resource, _ = legacy_resources
    policy = AgentKnowledgePolicy(
        agent_id=agent.id, knowledge_resource_id=resource.id, select_all=True,
        excluded_subjects_json=json.dumps([["USER", member.id]]),
    )
    session.add(policy)
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert not scope.allows(resource)

    policy.excluded_subjects_json = "[]"
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert scope.allows(resource)
    await session.delete(policy)
    agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert not scope.allows(resource)


@pytest.mark.parametrize("independent_principal", [False, True])
async def test_source_unbind_revokes_only_source_acl_context(
    legacy_resources, client, independent_principal
):
    session, member, agent, resource, _ = legacy_resources
    http, headers, _ = client
    source = EnterpriseIdentitySource.create(
        name="Bound source", provider=IdentitySourceProvider.FEISHU, tenant_id="tenant",
    )
    source.status = EnterpriseIdentitySourceStatus.ACTIVE
    source.last_synced_at = datetime.now(UTC)
    session.add(source)
    await session.flush()
    session.add_all([
        EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id, knowledge_space_id=resource.knowledge_space_id,
        ),
        UserExternalIdentity(
            user_id=member.id, identity_provider=identity_provider_key(source),
            external_subject="external-member",
        ),
        EnterpriseDirectoryUser(
            identity_source_id=source.id, external_user_id="external-member",
            display_name="Member", status="active",
        ),
    ])
    if independent_principal:
        session.add(EnterprisePrincipalAssignment(
            pas_user_id=member.id, identity_domain=resource.identity_domain,
            provider="feishu", principal_type="user", principal_id="independent-member",
            source="admin_managed", status="active",
        ))
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    before = await plan_knowledge_access(session, member, [resource.id], resource_scope=scope)
    assert {"provider": "feishu", "type": "user", "id": "external-member"} in before.acl_context["principals"]

    response = await http.delete(
        f"/api/identity-sources/{source.id}/spaces/{resource.knowledge_space_id}",
        headers=headers,
    )
    assert response.status_code == 204
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    if independent_principal:
        after = await plan_knowledge_access(session, member, [resource.id], resource_scope=scope)
        assert {"provider": "feishu", "type": "user", "id": "independent-member"} in after.acl_context["principals"]
        assert {"provider": "feishu", "type": "user", "id": "external-member"} not in after.acl_context["principals"]
    else:
        with pytest.raises(KnowledgeAccessError):
            await plan_knowledge_access(session, member, [resource.id], resource_scope=scope)
