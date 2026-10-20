from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentKnowledgePolicy,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentPolarRAGInstanceBinding,
    Department,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceSpaceBinding,
    IdentitySourceProvider,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceManagementMode,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    UserDepartment,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_explicit_parent_kb_grant_is_bound_for_child_group_and_options(client, setup):
    http, headers, _ = client
    factory, admin, _ = setup
    async with factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Engineering", provider=IdentitySourceProvider.FEISHU, tenant_id="manual-parent-source",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        agent = Agent(name="manual-parent-agent", created_by=admin.id, knowledge_scope_mode=AgentKnowledgeScopeMode.SCOPED)
        instance = PolarRAGInstance(
            name="manual-parent-rag", scheme="http", host="rag.example.test", port=9200,
            username_ciphertext="username", password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        session.add_all([source, agent, instance])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id, space_id="manual-parent-space", name="Nested",
            identity_domain="nested-domain", enabled=True,
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id=space.space_id, kb_id="manual-parent-kb", name="Nested KB", kb_type="PUBLIC",
            identity_domain=space.identity_domain, binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            management_mode=KnowledgeResourceManagementMode.NATIVE,
        )
        parent_assignment = AgentGroupAssignment.for_identity_source_group(
            agent_id=agent.id, identity_source_id=source.id,
            external_group_id="parent", created_by_user_id=admin.id,
        )
        session.add_all([
            resource, parent_assignment,
            EnterpriseIdentitySourceSpaceBinding(
                identity_source_id=source.id, knowledge_space_id=space.knowledge_space_id,
            ),
            AgentPolarRAGInstanceBinding(
                agent_id=agent.id, polarrag_instance_id=instance.id, created_by_user_id=admin.id,
            ),
            *[EnterpriseDirectoryGroup(
                identity_source_id=source.id, external_group_id=name, display_name=name,
                principal_type=EnterpriseDirectoryPrincipalType.GROUP, status="active",
            ) for name in ("parent", "middle", "child")],
            *[EnterpriseDirectoryMembership(
                identity_source_id=source.id, external_group_id=parent,
                member_type=EnterpriseDirectoryMembershipType.GROUP, external_member_id=child,
            ) for parent, child in (("parent", "middle"), ("middle", "child"))],
        ])
        await session.flush()
        scope = AgentKnowledgeScope(
            agent_id=agent.id, name="Parent KB", origin="MANUAL",
            knowledge_resource_ids_json=json.dumps([resource.id]), created_by_user_id=admin.id,
        )
        session.add(scope)
        await session.flush()
        session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, group_assignment_id=parent_assignment.id))
        await session.commit()
        agent_id, source_id, resource_id = agent.id, source.id, resource.id

    status = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-binding-status", headers=headers,
        json={"subjects": [{"type": "GROUP", "identity_source_id": source_id, "group_id": "child"}],
              "knowledge_resource_ids": [resource_id]},
    )
    assert status.status_code == 200
    row = status.json()["items"][0]
    assert row["status"] == "BOUND"
    assert row["direct_bound_subject_indexes"] == []
    assert row["inherited_subject_sources"] == {"0": ["Engineering"]}

    options = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
        json={"knowledge_resource_ids": [resource_id], "category": "groups",
              "binding_status": "bound", "search": "child"},
    )
    assert options.status_code == 200
    assert [(item["subject"]["group_id"], item["status"]) for item in options.json()["items"]] == [
        ("child", "BOUND")
    ]


@pytest.mark.parametrize("mode", [AgentKnowledgeScopeMode.LEGACY_ALL, AgentKnowledgeScopeMode.SCOPED])
async def test_subgroup_binding_status_inherits_assigned_ancestor(client, setup, mode):
    http, headers, _ = client
    factory, admin, _ = setup
    async with factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Engineering", provider=IdentitySourceProvider.FEISHU, tenant_id="nested-source",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        agent = Agent(name="nested-agent", created_by=admin.id, knowledge_scope_mode=mode)
        instance = PolarRAGInstance(
            name="nested-rag", scheme="http", host="rag.example.test", port=9200,
            username_ciphertext="username", password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        session.add_all([source, agent, instance])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id, space_id="nested-space", name="Nested",
            identity_domain="nested-domain", enabled=True,
        )
        session.add(space)
        await session.flush()
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id=space.space_id, kb_id="nested-kb", name="Nested KB", kb_type="PUBLIC",
            identity_domain=space.identity_domain, binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            management_mode=KnowledgeResourceManagementMode.NATIVE,
        )
        groups = [EnterpriseDirectoryGroup(
            identity_source_id=source.id, external_group_id=name, display_name=name,
            principal_type=EnterpriseDirectoryPrincipalType.GROUP, status="active",
        ) for name in ("parent", "middle", "child")]
        session.add_all([
            resource, *groups,
            EnterpriseIdentitySourceSpaceBinding(
                identity_source_id=source.id, knowledge_space_id=space.knowledge_space_id,
            ),
            AgentPolarRAGInstanceBinding(
                agent_id=agent.id, polarrag_instance_id=instance.id, created_by_user_id=admin.id,
            ),
            AgentGroupAssignment.for_identity_source_group(
                agent_id=agent.id, identity_source_id=source.id,
                external_group_id="parent", created_by_user_id=admin.id,
            ),
            *[EnterpriseDirectoryMembership(
                identity_source_id=source.id, external_group_id=parent,
                member_type=EnterpriseDirectoryMembershipType.GROUP, external_member_id=child,
            ) for parent, child in (("parent", "middle"), ("middle", "child"))],
        ])
        await session.flush()
        if mode == AgentKnowledgeScopeMode.SCOPED:
            session.add(AgentKnowledgePolicy(
                agent_id=agent.id, knowledge_resource_id=resource.id, select_all=True,
            ))
        await session.commit()
        agent_id, source_id, resource_id = agent.id, source.id, resource.id
        parent_id = groups[0].id

    subject = {"type": "GROUP", "identity_source_id": source_id, "group_id": "child"}
    body = {"subjects": [subject], "knowledge_resource_ids": [resource_id]}
    path = f"/api/admin/agents/{agent_id}/knowledge-binding-status"
    response = await http.post(path, headers=headers, json=body)
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["status"] == "BOUND"
    assert row["direct_bound_subject_indexes"] == [0]
    assert row["inherited_subject_sources"] == {
        "0": ["ALL_AGENT_KNOWLEDGE" if mode == AgentKnowledgeScopeMode.LEGACY_ALL else "SELECT_ALL"],
    }
    async with factory() as session:
        assignments = list(await session.scalars(select(AgentGroupAssignment).where(
            AgentGroupAssignment.agent_id == agent_id,
        )))
        assert [assignment.principal_id for assignment in assignments] == ["parent"]

    excluded = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-bindings:batch", headers=headers,
        json={"operations": [{"operation": "UNBIND", "subjects": [subject],
                               "targets": [{"knowledge_resource_id": resource_id}]}]},
    )
    assert excluded.status_code == 200
    response = await http.post(path, headers=headers, json=body)
    assert response.json()["items"][0]["status"] == "UNBOUND"
    assert response.json()["items"][0]["excluded_subject_indexes"] == [0]
    async with factory() as session:
        policy = await session.scalar(select(AgentKnowledgePolicy).where(
            AgentKnowledgePolicy.agent_id == agent_id,
            AgentKnowledgePolicy.knowledge_resource_id == resource_id,
        ))
        policy.excluded_subjects_json = "[]"
        parent = await session.get(EnterpriseDirectoryGroup, parent_id)
        parent.status = "inactive"
        await session.commit()
    response = await http.post(path, headers=headers, json=body)
    assert response.json()["items"][0]["status"] == "UNBOUND"


async def test_batch_bindings_support_multiple_operations_and_external_snapshot(
    client,
    setup,
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        department = Department(name="Scoped department")
        unassigned_department = Department(name="Unassigned department")
        agent = Agent(name="scoped-agent", created_by=admin.id)
        identity_source = EnterpriseIdentitySource.create(
            name="Scoped source",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-scoped-source",
        )
        identity_source.status = EnterpriseIdentitySourceStatus.ACTIVE
        identity_source.last_synced_at = datetime.now(UTC)
        instance = PolarRAGInstance(
            name="scoped-rag",
            scheme="http",
            host="rag.example.test",
            port=9200,
            username_ciphertext="username",
            password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add_all(
            [department, unassigned_department, agent, identity_source, instance]
        )
        await session.flush()
        session.add_all(
            [
                UserDepartment(
                    user_id=member.id,
                    department_id=department.id,
                    is_primary=True,
                ),
                EnterpriseDirectoryGroup(
                    identity_source_id=identity_source.id,
                    external_group_id="group-1",
                    display_name="Group 1",
                    principal_type=EnterpriseDirectoryPrincipalType.GROUP,
                    status="active",
                ),
            ]
        )
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="space-1",
            name="Space",
            identity_domain="domain-1",
            enabled=True,
        )
        session.add(space)
        await session.flush()
        resources = [
            KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=instance.id,
                space_id=space.space_id,
                kb_id=f"kb-{index}",
                name=f"KB {index}",
                kb_type="PUBLIC",
                identity_domain=space.identity_domain,
                binding_mode=KnowledgeBindingMode.DOMAIN,
                sync_status=KnowledgeResourceSyncStatus.ACTIVE,
                management_mode=(
                    KnowledgeResourceManagementMode.EXTERNAL_SYNC
                    if index == 2
                    else KnowledgeResourceManagementMode.NATIVE
                ),
            )
            for index in range(3)
        ]
        session.add_all(
            [
                *resources,
                EnterpriseIdentitySourceSpaceBinding(identity_source_id=identity_source.id, knowledge_space_id=space.knowledge_space_id),
                AgentPolarRAGInstanceBinding(
                    agent_id=agent.id,
                    polarrag_instance_id=instance.id,
                    created_by_user_id=admin.id,
                ),
            ]
        )
        await session.commit()
        agent_id = agent.id
        admin_id = admin.id
        user_id = member.id
        department_id = department.id
        unassigned_department_id = unassigned_department.id
        identity_source_id = identity_source.id
        resource_ids = [resource.id for resource in resources]

    batch = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-bindings:batch",
        headers=admin_headers,
        json={
            "operations": [
                {
                    "operation": "BIND",
                    "subjects": [
                        {"type": "USER", "user_id": user_id},
                        {"type": "USER", "user_id": admin_id},
                    ],
                    "targets": [
                        {"space_id": "space-1", "kb_id": "kb-0"},
                        {"knowledge_resource_id": resource_ids[1]},
                    ],
                },
                {
                    "operation": "UNBIND",
                    "subjects": [{"type": "USER", "user_id": user_id}],
                    "targets": [{"space_id": "space-1", "kb_id": "kb-0"}],
                },
                {
                    "operation": "UNBIND",
                    "subjects": [
                        {
                            "type": "DEPARTMENT",
                            "department_id": unassigned_department_id,
                        }
                    ],
                    "targets": [{"space_id": "space-1", "kb_id": "kb-0"}],
                },
                {
                    "operation": "BIND",
                    "subjects": [{"type": "DEPARTMENT", "department_id": department_id}],
                    "targets": [{"knowledge_resource_id": resource_ids[2]}],
                },
                {
                    "operation": "BIND",
                    "subjects": [
                        {
                            "type": "GROUP",
                            "identity_source_id": identity_source_id,
                            "group_id": "group-1",
                        }
                    ],
                    "targets": [{"knowledge_resource_id": resource_ids[2]}],
                },
            ],
            "activate_scoped_mode": True,
        },
    )
    assert batch.status_code == 200, batch.text
    assert batch.json() == {
        "agent_id": agent_id,
        "operations_processed": 5,
        "knowledge_scope_mode": "SCOPED",
    }

    listed = await http.get(
        f"/api/admin/agents/{agent_id}/knowledge-bindings",
        headers=admin_headers,
        params={"offset": 0, "limit": 10},
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["knowledge_scope_mode"] == "SCOPED"
    assert payload["total"] == 4
    assert payload["offset"] == 0
    assert payload["limit"] == 10
    by_subject = {
        (item["subject"]["type"], item["subject"]["display_name"]): {
            resource["kb_id"] for resource in item["knowledge_resources"]
        }
        for item in payload["items"]
    }
    assert by_subject == {
        ("USER", admin.display_name): {"kb-0", "kb-1"},
        ("USER", member.display_name): {"kb-1"},
        ("DEPARTMENT", "Scoped department"): {"kb-2"},
        ("GROUP", "Group 1"): {"kb-2"},
    }
    options = await http.get(
        f"/api/admin/agents/{agent_id}/knowledge-resource-options",
        headers=admin_headers,
        params={"offset": 0, "limit": 10},
    )
    assert options.status_code == 200
    assert options.json() == {
        "items": [
            {
                "knowledge_resource_id": resource_ids[index],
                "kb_type": "PUBLIC",
                "agent_bound": True,
                "space_id": "space-1",
                "space_name": "Space",
                "kb_id": f"kb-{index}",
                "name": f"KB {index}",
                "management_mode": (
                    "EXTERNAL_SYNC" if index == 2 else "NATIVE"
                ),
                "identity_source_id": identity_source_id,
                "identity_source_ids": [identity_source_id],
                "access_scope": "SELECTED",
                "inheritance_sources": ["Scoped department", "Scoped source"] if index == 2 else [],
            }
            for index in range(3)
        ],
        "total": 3,
        "offset": 0,
        "limit": 10,
    }
    native_options = await http.get(
        f"/api/admin/agents/{agent_id}/knowledge-resource-options",
        headers=admin_headers,
        params={"offset": 0, "limit": 1, "management_mode": "NATIVE"},
    )
    assert native_options.status_code == 200
    assert native_options.json()["total"] == 3
    assert native_options.json()["limit"] == 1
    assert native_options.json()["items"][0]["management_mode"] == "NATIVE"

    statuses = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-binding-status",
        headers=admin_headers,
        json={
            "subjects": [
                {"type": "USER", "user_id": user_id},
                {"type": "USER", "user_id": admin_id},
            ],
            "knowledge_resource_ids": resource_ids[:2],
        },
    )
    assert statuses.status_code == 200
    assert statuses.json() == {
        "items": [
            {
                "knowledge_resource_id": resource_ids[0],
                "space_id": "space-1",
                "kb_id": "kb-0",
                "status": "PARTIAL",
                "bound_subject_count": 1,
                "subject_count": 2,
                "bound_subject_indexes": [1],
                "direct_bound_subject_indexes": [1],
                "partial_subject_indexes": [],
                "excluded_subject_indexes": [],
                "inherited_subject_sources": {},
            },
            {
                "knowledge_resource_id": resource_ids[1],
                "space_id": "space-1",
                "kb_id": "kb-1",
                "status": "BOUND",
                "bound_subject_count": 2,
                "subject_count": 2,
                "bound_subject_indexes": [0, 1],
                "direct_bound_subject_indexes": [0, 1],
                "partial_subject_indexes": [],
                "excluded_subject_indexes": [],
                "inherited_subject_sources": {},
            },
        ]
    }

    source_assignment = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        headers=admin_headers,
        json={
            "group_kind": "identity_source_all",
            "identity_source_id": identity_source_id,
        },
    )
    assert source_assignment.status_code == 201
    source_binding = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-bindings:batch",
        headers=admin_headers,
        json={
            "operations": [
                {
                    "operation": "BIND",
                    "subjects": [
                        {
                            "type": "IDENTITY_SOURCE",
                            "identity_source_id": identity_source_id,
                        }
                    ],
                    "targets": [{"knowledge_resource_id": resource_ids[0]}],
                }
            ]
        },
    )
    assert source_binding.status_code == 200
    source_status = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-binding-status",
        headers=admin_headers,
        json={
            "subjects": [
                {
                    "type": "IDENTITY_SOURCE",
                    "identity_source_id": identity_source_id,
                }
            ],
            "knowledge_resource_ids": [resource_ids[0]],
        },
    )
    assert source_status.status_code == 200
    assert source_status.json()["items"][0]["status"] == "BOUND"

    inherited_status = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-binding-status",
        headers=admin_headers,
        json={
            "subjects": [{"type": "GROUP", "identity_source_id": identity_source_id, "group_id": "group-1"}],
            "knowledge_resource_ids": resource_ids[:2],
        },
    )
    assert inherited_status.status_code == 200
    assert inherited_status.json()["items"][0]["bound_subject_indexes"] == [0]
    assert inherited_status.json()["items"][0]["direct_bound_subject_indexes"] == []
    assert inherited_status.json()["items"][0]["inherited_subject_sources"] == {"0": ["Scoped source"]}
    assert inherited_status.json()["items"][1]["status"] == "UNBOUND"

    removed = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-bindings:batch",
        headers=admin_headers,
        json={"operations": [{
            "operation": "UNBIND",
            "subjects": [{"type": "USER", "user_id": admin_id}],
            "targets": [{"knowledge_resource_id": resource_ids[0]}],
        }]},
    )
    assert removed.status_code == 200
    after_remove = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-binding-status",
        headers=admin_headers,
        json={"subjects": [{"type": "USER", "user_id": admin_id}], "knowledge_resource_ids": resource_ids[:2]},
    )
    assert [item["status"] for item in after_remove.json()["items"]] == ["UNBOUND", "BOUND"]
    restored = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-bindings:batch",
        headers=admin_headers,
        json={"operations": [{
            "operation": "BIND", "subjects": [{"type": "USER", "user_id": admin_id}],
            "targets": [{"knowledge_resource_id": resource_ids[0]}],
        }]},
    )
    assert restored.status_code == 200

    from server.core.agent_knowledge_scope import (
        resolve_agent_knowledge_resource_scope,
    )

    async with factory() as session:
        member_scope = await resolve_agent_knowledge_resource_scope(
            session,
            agent_id,
            user_id,
        )
        admin_scope = await resolve_agent_knowledge_resource_scope(
            session,
            agent_id,
            admin_id,
        )
        assert member_scope.allowed_resource_ids == {
            resource_ids[1],
            resource_ids[2],
        }
        assert admin_scope.allowed_resource_ids == set(resource_ids[:2])
        group = (
            await session.execute(
                select(AgentGroupAssignment).where(
                    AgentGroupAssignment.agent_id == agent_id,
                    AgentGroupAssignment.identity_source_id == identity_source_id,
                    AgentGroupAssignment.principal_id == "group-1",
                )
            )
        ).scalar_one()
        assert group is not None
        assert not await session.scalar(
            select(AgentGroupAssignment).where(
                AgentGroupAssignment.agent_id == agent_id,
                AgentGroupAssignment.department_id == unassigned_department_id,
            )
        )

    external_path = (
        f"/api/admin/identity-sources/{identity_source_id}/agents/{agent_id}/knowledge-bindings/wiki-space-1"
    )
    external = await http.put(
        external_path,
        headers=admin_headers,
        json={
            "subjects": [{"type": "USER", "user_id": user_id}],
            "targets": [
                {"space_id": "space-1", "kb_id": "kb-0"},
                {"knowledge_resource_id": resource_ids[1]},
            ],
            "activate_scoped_mode": True,
        },
    )
    assert external.status_code == 200
    assert external.json()["external_scope_id"] == "wiki-space-1"

    replaced = await http.put(
        external_path,
        headers=admin_headers,
        json={
            "subjects": [{"type": "USER", "user_id": user_id}],
            "targets": [{"knowledge_resource_id": resource_ids[2]}],
        },
    )
    assert replaced.status_code == 200

    async with factory() as session:
        external_scopes = list(
            (
                await session.execute(
                    select(AgentKnowledgeScope).where(AgentKnowledgeScope.identity_source_id == identity_source_id)
                )
            ).scalars()
        )
        assert len(external_scopes) == 1
        assert json.loads(external_scopes[0].knowledge_resource_ids_json) == [resource_ids[2]]

    deleted = await http.delete(external_path, headers=admin_headers)
    assert deleted.status_code == 204

    legacy = await http.put(
        f"/api/admin/agents/{agent_id}/knowledge-scope-mode",
        headers=admin_headers,
        json={"mode": "LEGACY_ALL"},
    )
    assert legacy.status_code == 200
    legacy_status = await http.post(
        f"/api/admin/agents/{agent_id}/knowledge-binding-status",
        headers=admin_headers,
        json={"subjects": [{"type": "USER", "user_id": admin_id}], "knowledge_resource_ids": [resource_ids[0]]},
    )
    assert legacy_status.json()["items"][0]["direct_bound_subject_indexes"] == [0]
    assert legacy_status.json()["items"][0]["inherited_subject_sources"] == {"0": ["ALL_AGENT_KNOWLEDGE"]}
    legacy_options = await http.get(
        f"/api/admin/agents/{agent_id}/knowledge-resource-options", headers=admin_headers,
    )
    assert all(item["access_scope"] == "ALL_USERS" for item in legacy_options.json()["items"])

    async with factory() as session:
        from server.models import AgentKnowledgeScopeMode, AgentKnowledgeScopeBinding
        from server.api.agent_knowledge_scopes import _resource_access_summaries

        inherited_agent = Agent(name="inherited-only", created_by=admin.id, knowledge_scope_mode=AgentKnowledgeScopeMode.SCOPED)
        session.add(inherited_agent)
        await session.flush()
        all_source = AgentGroupAssignment.for_identity_source_all_users(
            agent_id=inherited_agent.id, identity_source_id=identity_source_id, created_by_user_id=admin.id,
        )
        source_group = AgentGroupAssignment.for_identity_source_group(
            agent_id=inherited_agent.id, identity_source_id=identity_source_id, external_group_id="group-1", created_by_user_id=admin.id,
        )
        session.add_all([all_source, source_group])
        scope = AgentKnowledgeScope(agent_id=inherited_agent.id, name="Source access", origin="MANUAL", knowledge_resource_ids_json=json.dumps([resource_ids[0]]), created_by_user_id=admin.id)
        session.add(scope)
        await session.flush()
        session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, group_assignment_id=all_source.id))
        await session.flush()
        summaries = await _resource_access_summaries(session, inherited_agent, set(resource_ids[:2]))
        assert summaries[resource_ids[0]] == {"access_scope": "ALL_USERS", "inheritance_sources": ["Scoped source"]}
        assert summaries[resource_ids[1]] == {"access_scope": "UNCONFIGURED", "inheritance_sources": []}
        inherited_agent.all_builtin_users = True
        await session.flush()
        summaries = await _resource_access_summaries(session, inherited_agent, set(resource_ids[:2]))
        assert summaries[resource_ids[0]]["access_scope"] == "SELECTED"
        assert summaries[resource_ids[1]]["access_scope"] == "UNCONFIGURED"


async def test_enterprise_user_subjects_batch_status_scope_and_outputs(client, setup):
    from server.enterprise_identity.service import identity_provider_key
    from server.models import AuditLog, UserExternalIdentity
    from sqlalchemy import func
    from tests.test_agent_enterprise_access_api import _seed_enterprise_access

    http, headers, member_headers = client
    factory, admin, member = setup
    seeded = await _seed_enterprise_access(setup)
    async with factory() as session:
        agent = await session.get(Agent, seeded.agent_id)
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
        space = await session.get(PolarRAGSpace, seeded.space_id)
        resource = KnowledgeResource(knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=seeded.bound_instance_id,
                                     space_id=space.space_id, kb_id="enterprise-kb", name="Enterprise KB", kb_type="PUBLIC",
                                     identity_domain=space.identity_domain,
                                     binding_mode=KnowledgeBindingMode.DOMAIN, sync_status=KnowledgeResourceSyncStatus.ACTIVE,
                                     management_mode=KnowledgeResourceManagementMode.NATIVE)
        other = await session.get(EnterpriseIdentitySource, seeded.other_source_id)
        session.add_all([resource, EnterpriseIdentitySourceSpaceBinding(identity_source_id=seeded.source_id,
                                                                        knowledge_space_id=seeded.space_id),
                         UserExternalIdentity(user_id=member.id, identity_provider=identity_provider_key(other),
                                              external_subject="member-other")])
        await session.flush()
        binding = (await session.execute(select(AgentPolarRAGInstanceBinding).where(AgentPolarRAGInstanceBinding.agent_id == seeded.agent_id))).scalar_one()
        binding.public_knowledge_resource_ids_json = json.dumps([resource.id])
        await session.commit()
        resource_id = resource.id
    base = f"/api/admin/agents/{seeded.agent_id}"
    visibility = await http.get(f"{base}/knowledge-visibility", headers=headers)
    assert visibility.status_code == 200
    assert visibility.json()["instance_count"] == 1
    assert set(visibility.json()["statuses"]) == {"PUBLIC", "PERSONAL"}
    old = {"type": "USER", "user_id": member.id}
    new = {"type": "USER", "identity_source_id": seeded.source_id, "external_user_id": "member-subject"}
    identities = sorted([{"identity_source_id": seeded.source_id, "external_user_id": "member-subject"},
                         {"identity_source_id": seeded.other_source_id, "external_user_id": "member-other"}],
                        key=lambda item: item["identity_source_id"])
    target = {"knowledge_resource_id": resource_id}
    def batch(subjects, operation="BIND"):
        return {"operations": [{"operation": operation, "subjects": subjects, "targets": [target]}]}
    bound = await http.post(f"{base}/knowledge-bindings:batch", headers=headers, json=batch([old, new]))
    assert bound.status_code == 200, bound.text
    async with factory() as session:
        rows = (await session.execute(select(AgentKnowledgeScopeBinding))).scalars().all()
        assert len(rows) == 1 and rows[0].user_id == member.id
        count = await session.scalar(select(func.count()).select_from(AuditLog))
    old_status = await http.post(f"{base}/knowledge-binding-status", headers=headers,
                                 json={"subjects": [old], "knowledge_resource_ids": [resource_id]})
    new_status = await http.post(f"{base}/knowledge-binding-status", headers=headers,
                                 json={"subjects": [new], "knowledge_resource_ids": [resource_id]})
    assert old_status.status_code == new_status.status_code == 200
    assert old_status.json() == new_status.json()
    assert new_status.json()["items"][0]["status"] == "BOUND"
    for subject, status in (({**new, "external_user_id": "missing"}, 404),
                            ({**new, "user_id": member.id}, 422),
                            ({"type": "USER", "identity_source_id": seeded.source_id}, 422)):
        rejected = await http.post(f"{base}/knowledge-bindings:batch", headers=headers, json=batch([{"type": "USER", "user_id": admin.id}, subject]))
        assert rejected.status_code == status, rejected.text
    assert (await http.post(f"{base}/knowledge-bindings:batch", headers=member_headers, json=batch([new]))).status_code == 403
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentKnowledgeScopeBinding)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == count
    listed = await http.get(f"{base}/knowledge-bindings", headers=headers, params={"limit": 1})
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["subject"]["enterprise_identities"] == identities
    options = await http.post(f"{base}/knowledge-subject-options", headers=headers,
                              json={"knowledge_resource_ids": [resource_id], "category": "users",
                                    "binding_status": "bound", "search": "Member", "limit": 1})
    assert options.status_code == 200, options.text
    assert options.json()["items"][0]["enterprise_identities"] == identities
    assert (await http.post(f"{base}/knowledge-bindings:batch", headers=headers, json=batch([old, new], "UNBIND"))).status_code == 200
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentKnowledgeScopeBinding)) == 0
    scope_url = f"/api/admin/identity-sources/{seeded.source_id}/agents/{seeded.agent_id}/knowledge-bindings/external-scope"
    other_source_user = {"type": "USER", "identity_source_id": seeded.other_source_id, "external_user_id": "member-other"}
    for subject in (old, new, other_source_user):
        scope = await http.put(scope_url, headers=headers, json={"subjects": [subject], "targets": [target]})
        assert scope.status_code == 200, scope.text
        assert scope.json()["user_ids"] == [member.id]
        assert scope.json()["enterprise_users"] == [{"user_id": member.id, "enterprise_identities": identities}]
    assert (await http.delete(scope_url, headers=headers)).status_code == 204


def test_enterprise_user_subject_shape_keeps_other_subject_types():
    from pydantic import ValidationError
    from server.api.agent_knowledge_scopes import KnowledgeBindingSubject

    assert KnowledgeBindingSubject(type="USER", user_id="pas-user").key() == ("USER", "pas-user")
    KnowledgeBindingSubject(type="USER", identity_source_id="source", external_user_id="enterprise-user")
    KnowledgeBindingSubject(type="GROUP", identity_source_id="source", group_id="group")
    KnowledgeBindingSubject(type="DEPARTMENT", department_id="department")
    KnowledgeBindingSubject(type="IDENTITY_SOURCE", identity_source_id="source")
    for payload in ({"type": "USER", "identity_source_id": "source"},
                    {"type": "USER", "user_id": "pas", "identity_source_id": "source", "external_user_id": "external"},
                    {"type": "GROUP", "identity_source_id": "source", "group_id": "group", "external_user_id": "external"}):
        with pytest.raises(ValidationError):
            KnowledgeBindingSubject(**payload)
