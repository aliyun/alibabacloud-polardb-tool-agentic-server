from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentKnowledgeScope,
    AgentPolarRAGInstanceBinding,
    Department,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    UserDepartment,
)

pytest_plugins = ("tests._admin_api_fixtures",)


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
            )
            for index in range(3)
        ]
        session.add_all(
            [
                *resources,
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
    assert batch.status_code == 200
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
                "space_id": "space-1",
                "space_name": "Space",
                "kb_id": f"kb-{index}",
                "name": f"KB {index}",
            }
            for index in range(3)
        ],
        "total": 3,
        "offset": 0,
        "limit": 10,
    }

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
