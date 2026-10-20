from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope
from server.models import (
    Agent, AgentGroupAssignment, AgentKnowledgePolicy, AgentKnowledgeScope,
    AgentKnowledgeScopeBinding, AgentKnowledgeScopeMode, AgentKnowledgeScopeOrigin, AgentPolarRAGInstanceBinding,
    Department, KnowledgeBindingMode, KnowledgeResource, KnowledgeResourceSyncStatus,
    PolarRAGInstance, PolarRAGInstanceStatus, PolarRAGSpace, UserDepartment,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture(params=[AgentKnowledgeScopeOrigin.MANUAL, AgentKnowledgeScopeOrigin.EXTERNAL_SYNC])
async def migration(setup, request):
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="Migration", created_by=admin.id)
        instance = PolarRAGInstance(name="RAG", scheme="http", host="rag.test", port=9200,
            username_ciphertext="u", password_ciphertext="p", status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id)
        department = Department(name="Engineering")
        session.add_all([agent, instance, department])
        await session.flush()
        space = PolarRAGSpace(polarrag_instance_id=instance.id, space_id="space", name="Space",
            identity_domain="domain", enabled=True)
        session.add(space)
        await session.flush()
        resources = [KnowledgeResource(knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=instance.id, space_id="space", kb_id=f"kb-{i}", name=f"KB {i}",
            kb_type="PUBLIC", identity_domain="domain", binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE) for i in range(4)]
        session.add_all(resources)
        await session.flush()
        assignment = AgentGroupAssignment.for_department(agent_id=agent.id,
            department_id=department.id, created_by_user_id=admin.id)
        scope = AgentKnowledgeScope(agent_id=agent.id, name="Existing bindings", origin=request.param,
            knowledge_resource_ids_json=json.dumps([resources[1].id]))
        session.add_all([assignment, scope, UserDepartment(user_id=member.id, department_id=department.id),
            AgentPolarRAGInstanceBinding(agent_id=agent.id, polarrag_instance_id=instance.id,
                public_excluded_resource_ids_json=json.dumps([resources[3].id]), created_by_user_id=admin.id),
            AgentKnowledgePolicy(agent_id=agent.id, knowledge_resource_id=resources[2].id,
                select_all=True, excluded_subjects_json=json.dumps([["USER", member.id]]))])
        await session.flush()
        session.add_all([AgentKnowledgeScopeBinding.for_group(scope_id=scope.id, group_assignment_id=assignment.id),
            AgentKnowledgeScopeBinding.for_user(scope_id=scope.id, user_id=admin.id)])
        await session.commit()
        return agent.id, [r.id for r in resources], scope.id


async def scopes(factory, aid, users):
    async with factory() as session:
        resources = list(await session.scalars(select(KnowledgeResource)))
        result = []
        for uid in users:
            scope = await resolve_agent_knowledge_resource_scope(session, aid, uid)
            result.append({resource.id for resource in resources if scope.allows(resource)})
        return result


@pytest.mark.parametrize("auto_bind", [False, True])
async def test_round_trip_removes_derived_defaults_and_keeps_manual(client, setup, migration, auto_bind):
    http, headers, _ = client
    factory, admin, member = setup
    aid, rids, scope_id = migration
    async with factory() as session:
        origin = (await session.get(AgentKnowledgeScope, scope_id)).origin
    response = await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
        json={"mode": "SCOPED", "expected_mode": "LEGACY_ALL", "external_knowledge_auto_bind_enabled": auto_bind})
    assert response.status_code == 200
    assert response.json()["preserved_knowledge_resources"] == 0
    actual = await scopes(factory, aid, [admin.id, member.id])
    assert rids[0] not in actual[0]  # Legacy implicit default is not copied.
    assert (rids[1] in actual[0]) == (origin == AgentKnowledgeScopeOrigin.MANUAL)
    assert (rids[1] in actual[1]) == (origin == AgentKnowledgeScopeOrigin.MANUAL)
    assert rids[2] in actual[0] and rids[2] not in actual[1]
    async with factory() as session:
        assert await session.get(AgentKnowledgePolicy, (aid, rids[0])) is None
        assert (await session.get(AgentKnowledgePolicy, (aid, rids[2]))).select_all
    # Admin explicitly approves resetting ambiguous All Agent users policies.
    reset = await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
        json={"mode": "SCOPED", "expected_mode": "SCOPED", "reset_all_users_policies": True})
    assert reset.status_code == 200 and reset.json()["removed_all_users_policies"] == 1
    assert rids[2] not in (await scopes(factory, aid, [admin.id]))[0]
    legacy = await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
        json={"mode": "LEGACY_ALL", "expected_mode": "SCOPED"})
    assert legacy.status_code == 200 and not legacy.json()["enabled"]
    assert rids[0] in (await scopes(factory, aid, [admin.id]))[0]
    again = await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
        json={"mode": "SCOPED", "expected_mode": "LEGACY_ALL", "external_knowledge_auto_bind_enabled": auto_bind})
    assert again.status_code == 200
    assert rids[0] not in (await scopes(factory, aid, [admin.id]))[0]
    stale = await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
        json={"mode": "SCOPED", "expected_mode": "LEGACY_ALL"})
    assert stale.status_code == 409


async def test_failed_switch_rolls_back_all_writes(client, setup, migration, monkeypatch):
    http, headers, _ = client
    factory, _, _ = setup
    aid, rids, _ = migration
    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr("server.api.agent_knowledge_scopes._audit_agent", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
            json={"mode": "SCOPED", "external_knowledge_auto_bind_enabled": True})
    async with factory() as session:
        agent = await session.get(Agent, aid)
        assert agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL
        assert not agent.external_knowledge_auto_bind_enabled
        assert await session.get(AgentKnowledgePolicy, (aid, rids[0])) is None


async def test_switch_admin_only_and_reject_legacy_auto_bind(client, migration):
    http, headers, member_headers = client
    aid, _, _ = migration
    assert (await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=member_headers,
        json={"mode": "SCOPED"})).status_code == 403
    assert (await http.put(f"/api/admin/agents/{aid}/knowledge-scope-mode", headers=headers,
        json={"mode": "LEGACY_ALL", "external_knowledge_auto_bind_enabled": True})).status_code == 422


async def test_repairs_legacy_retry_once_and_preserves_valid_backoff_and_lease(setup, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from server.config import EnterpriseIdentitySyncConfig
    from server.enterprise_identity.sync import repair_excessive_group_retry_delays
    from server.models import EnterpriseIdentitySource, EnterpriseIdentitySourceStatus, IdentitySourceProvider
    from types import SimpleNamespace
    factory, _, _ = setup
    now = datetime.now(UTC)
    monkeypatch.setattr("server.enterprise_identity.sync.get_config", lambda: SimpleNamespace(
        enterprise_identity_sync=EnterpriseIdentitySyncConfig(feishu_group_and_knowledge_scope_interval_seconds=600)))
    ids=[]
    async with factory() as session:
        for i, delay in enumerate([86400, 600, 1800, 86400]):
            source=EnterpriseIdentitySource.create(name=str(i),provider=IdentitySourceProvider.FEISHU,tenant_id=str(i))
            source.status=EnterpriseIdentitySourceStatus.ACTIVE
            source.last_group_error="ReadTimeout"
            source.group_next_retry_at=now+timedelta(seconds=delay)
            if i == 3:
                source.sync_lease_until = now + timedelta(minutes=2)
            session.add(source)
            await session.flush()
            ids.append(source.id)
        await session.commit()
    assert await repair_excessive_group_retry_delays(factory, now)==1
    assert await repair_excessive_group_retry_delays(factory, now+timedelta(seconds=1))==0
    async with factory() as session:
        for i, delay in enumerate([600,600,1800,86400]):
            source=await session.get(EnterpriseIdentitySource,ids[i])
            assert source.group_next_retry_at.replace(tzinfo=UTC)==now+timedelta(seconds=delay)
            assert source.status==EnterpriseIdentitySourceStatus.ACTIVE
