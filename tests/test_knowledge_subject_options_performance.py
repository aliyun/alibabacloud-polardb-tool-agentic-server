import json
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest
from sqlalchemy import event, select

from server.models import (
    Agent, AgentGroupAssignment, AgentKnowledgePolicy, AgentKnowledgeScope, AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode, AgentPolarRAGInstanceBinding, AuthProvider,
    EnterpriseDirectoryGroup, EnterpriseDirectoryMembership, EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType, EnterpriseIdentitySource, EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus, IdentitySourceProvider,
    KnowledgeBindingMode, KnowledgeResource, KnowledgeResourceManagementMode,
    KnowledgeResourceSyncStatus, PolarRAGInstance, PolarRAGInstanceStatus,
    PolarRAGSpace, User,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.mark.parametrize(("user_count", "unbound_count"), [(300, 0), (3000, 5)])
async def test_most_users_bound_query_budget(client, setup, user_count, unbound_count):
    http, headers, _ = client
    factory, admin, _ = setup
    async with factory() as session:
        agent = Agent(name="bulk-options", created_by=admin.id, all_builtin_users=True,
                      knowledge_scope_mode=AgentKnowledgeScopeMode.LEGACY_ALL)
        instance = PolarRAGInstance(
            name="bulk-rag", scheme="http", host="rag.example.test", port=9200,
            username_ciphertext="username", password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        session.add_all([agent, instance])
        await session.flush()
        space = PolarRAGSpace(polarrag_instance_id=instance.id, space_id="bulk-space",
                              name="Bulk", identity_domain="bulk", enabled=True)
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id=space.space_id, kb_id="bulk-kb", name="Bulk", kb_type="PUBLIC",
            identity_domain="bulk", binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            management_mode=KnowledgeResourceManagementMode.NATIVE,
        )
        session.add(resource)
        source = EnterpriseIdentitySource.create(name="Bulk source", provider=IdentitySourceProvider.FEISHU,
                                                 tenant_id="bulk")
        session.add(source)
        await session.flush()
        session.add(EnterpriseIdentitySourceSpaceBinding(identity_source_id=source.id,
                                                        knowledge_space_id=space.knowledge_space_id))
        session.add(AgentPolarRAGInstanceBinding(agent_id=agent.id, polarrag_instance_id=instance.id,
                                                created_by_user_id=admin.id))
        now = datetime.now(UTC)
        session.add_all([
            User(external_id=f"bulk-{i}", display_name=f"Bulk {i:04}",
                 auth_provider=AuthProvider.OIDC if i < unbound_count else AuthProvider.BUILTIN,
                 created_at=now + timedelta(seconds=i))
            for i in range(user_count)
        ])
        await session.commit()
        agent_id, resource_id = agent.id, resource.id
    statements = []
    engine = factory.kw["bind"]

    def record(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    start = perf_counter()
    try:
        response = await http.post(
            f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
            json={"knowledge_resource_ids": [resource_id], "category": "users",
                  "binding_status": "available", "limit": 20},
        )
    finally:
        elapsed = perf_counter() - start
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    assert [item["label"] for item in response.json()["items"]] == [
        f"Bulk {i:04}" for i in reversed(range(unbound_count))
    ]
    assert all(item["status"] == "UNBOUND" for item in response.json()["items"])
    assert response.json()["next_offset"] is None
    counts = sum("count(" in sql.lower() for sql, _ in statements)
    print(f"options: users={user_count + 2} elapsed={elapsed:.3f}s SQL={len(statements)} counts={counts}")
    async with engine.connect() as connection:
        for sql, params in statements:
            if "ORDER BY users.created_at" in sql:
                plan = await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)
                print("candidate plan:", plan.all())
                break
    assert len(statements) < 50
    assert counts == 0


@pytest.mark.parametrize("group_count", [40, 400])
@pytest.mark.parametrize(("target_granted", "mode"), [
    (False, AgentKnowledgeScopeMode.SCOPED),
    (True, AgentKnowledgeScopeMode.SCOPED),
    (False, AgentKnowledgeScopeMode.LEGACY_ALL),
])
async def test_group_options_query_budget_ignores_unrelated_kb_grants(client, setup, group_count, target_granted, mode):
    http, headers, _ = client
    factory, admin, _ = setup
    async with factory() as session:
        agent = Agent(name="group-options", created_by=admin.id, knowledge_scope_mode=mode)
        source = EnterpriseIdentitySource.create(name="Groups", provider=IdentitySourceProvider.FEISHU,
                                                 tenant_id="group-options")
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        instance = PolarRAGInstance(name="groups-rag", scheme="http", host="rag.example.test", port=9200,
                                    username_ciphertext="username", password_ciphertext="password",
                                    status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id)
        session.add_all([agent, source, instance])
        await session.flush()
        space = PolarRAGSpace(polarrag_instance_id=instance.id, space_id="group-space", name="Groups",
                              identity_domain="groups", enabled=True)
        session.add(space)
        await session.flush()
        resources = [KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id=space.space_id, kb_id=kb_id, name=kb_id, kb_type="PUBLIC", identity_domain="groups",
            binding_mode=KnowledgeBindingMode.DOMAIN, sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            management_mode=KnowledgeResourceManagementMode.NATIVE,
        ) for kb_id in ("target", "unrelated")]
        parent = AgentGroupAssignment.for_identity_source_group(
            agent_id=agent.id, identity_source_id=source.id, external_group_id="parent", created_by_user_id=admin.id,
        )
        session.add_all([
            *resources, parent,
            AgentGroupAssignment.for_identity_source_all_users(
                agent_id=agent.id, identity_source_id=source.id, created_by_user_id=admin.id,
            ),
            EnterpriseIdentitySourceSpaceBinding(identity_source_id=source.id,
                                                  knowledge_space_id=space.knowledge_space_id),
            AgentPolarRAGInstanceBinding(agent_id=agent.id, polarrag_instance_id=instance.id,
                                         created_by_user_id=admin.id),
            *[EnterpriseDirectoryGroup(identity_source_id=source.id, external_group_id=group_id,
                                       display_name=group_id, principal_type=EnterpriseDirectoryPrincipalType.GROUP,
                                       status="active")
              for group_id in ["parent", "middle", *[f"leaf-{i:04}" for i in range(group_count)]]],
            *[EnterpriseDirectoryMembership(identity_source_id=source.id, external_group_id=ancestor,
                                            member_type=EnterpriseDirectoryMembershipType.GROUP,
                                            external_member_id=child)
              for ancestor, child in [("parent", "middle"),
                                       *[("middle", f"leaf-{i:04}") for i in range(group_count)]]],
        ])
        await session.flush()
        if mode == AgentKnowledgeScopeMode.LEGACY_ALL:
            session.add(AgentKnowledgePolicy(agent_id=agent.id, knowledge_resource_id=resources[0].id,
                                             select_all=False))
        scope = AgentKnowledgeScope(agent_id=agent.id, name="Parent grant", origin="MANUAL",
                                    knowledge_resource_ids_json=json.dumps([resources[0 if target_granted else 1].id]),
                                    created_by_user_id=admin.id)
        session.add(scope)
        await session.flush()
        session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, group_assignment_id=parent.id))
        await session.commit()
        agent_id = agent.id

    statements = []
    engine = factory.kw["bind"].sync_engine

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await http.post(
            f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
            json={"targets": [{"space_id": "group-space", "kb_id": "target"}], "category": "groups",
                  "binding_status": "bound", "limit": 100},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["items"]) == (min(group_count + 2, 100) if target_granted else 0)
    assert all(item["status"] == "BOUND" for item in payload["items"])
    assert (payload["next_offset"] is not None) == (target_granted and group_count + 2 > 100)
    assert len(statements) < (100 if target_granted else 30)
    if mode == AgentKnowledgeScopeMode.LEGACY_ALL:
        body = {"targets": [{"space_id": "group-space", "kb_id": kb_id} for kb_id in ("target", "unrelated")],
                "category": "groups", "binding_status": "bound", "limit": 100}
        mixed = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers, json=body)
        assert mixed.status_code == 200, mixed.text
        mixed_payload = mixed.json()
        assert len(mixed_payload["items"]) == min(group_count + 2, 100)
        assert all(item["status"] == "PARTIAL" for item in mixed_payload["items"])
        if group_count + 2 > 100:
            assert mixed_payload["next_offset"] is not None
            second = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
                                     json={**body, "offset": mixed_payload["next_offset"]})
            assert second.status_code == 200, second.text
            assert len(second.json()["items"]) == 100
            assert all(item["status"] == "PARTIAL" for item in second.json()["items"])
            first_ids = {item["subject"]["group_id"] for item in mixed_payload["items"]}
            assert not first_ids & {item["subject"]["group_id"] for item in second.json()["items"]}
        else:
            assert mixed_payload["next_offset"] is None
    async with factory() as session:
        assert len(list(await session.scalars(select(AgentGroupAssignment).where(
            AgentGroupAssignment.agent_id == agent_id,
        )))) == 2
