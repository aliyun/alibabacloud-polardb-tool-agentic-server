from __future__ import annotations

import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from server.api.external_knowledge_scopes import list_external_user_knowledge_resources
from server.models import (
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeOrigin,
    AgentKnowledgeScopeMode,
    AgentKnowledgePolicy,
    AgentUserAssignment,
    AgentStatus,
    UserStatus,
    KnowledgeResourceSyncStatus,
    AgentPolarRAGInstanceBinding,
    EnterpriseDirectoryUser,
)
from server.core.external_knowledge_scope_sync import import_external_knowledge_scope_snapshot
from tests.test_external_knowledge_scope_sync import _snapshot

pytest_plugins = ("tests.test_external_knowledge_scope_sync", "tests._admin_api_fixtures")


@pytest.fixture(autouse=True)
async def readable_identity(seeded):
    session, source, agent, member, resource = seeded
    session.add(
        EnterpriseDirectoryUser(
            identity_source_id=source.id, external_user_id="ou-member", display_name="Member", status="active"
        )
    )
    await session.commit()


async def page(seeded, origin="ALL", **kwargs):
    session, source, agent, member, resource = seeded
    return await list_external_user_knowledge_resources(
        source.id,
        agent.id,
        "ou-member",
        access_origin=origin,
        cursor=kwargs.get("cursor"),
        limit=kwargs.get("limit", 50),
        _admin=member,
        session=session,
    )


@pytest.mark.parametrize("kind", ["manual", "select_all", "legacy", "external"])
async def test_all_includes_effective_grant_sources(seeded, kind):
    session, source, agent, member, resource = seeded
    if kind == "manual":
        scope = AgentKnowledgeScope(
            agent_id=agent.id,
            name="manual",
            origin=AgentKnowledgeScopeOrigin.MANUAL,
            knowledge_resource_ids_json=json.dumps([resource.id]),
        )
        session.add(scope)
        await session.flush()
        session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, user_id=member.id))
    elif kind == "select_all":
        session.add(
            AgentKnowledgePolicy(
                agent_id=agent.id, knowledge_resource_id=resource.id, select_all=True, excluded_subjects_json="[]"
            )
        )
    elif kind == "legacy":
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.LEGACY_ALL
    else:
        await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    assert [x.knowledge_resource_id for x in (await page(seeded)).items] == [resource.id]
    assert bool((await page(seeded, "EXTERNAL_SYNC")).items) == (kind == "external")
    assert bool((await page(seeded, "MANUAL")).items) == (kind in {"manual", "select_all"})
    with pytest.raises(HTTPException) as exc:
        await page(seeded, cursor="not-a-resource")
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "guard",
    [
        "no_agent_access",
        "inactive_agent",
        "inactive_user",
        "disabled_resource",
        "outside_global_range",
        "policy_exclusion",
    ],
)
async def test_all_obeys_effective_access_guards(seeded, guard):
    session, source, agent, member, resource = seeded
    agent.knowledge_scope_mode = AgentKnowledgeScopeMode.LEGACY_ALL
    if guard == "no_agent_access":
        await session.delete(await session.scalar(select(AgentUserAssignment)))
    elif guard == "inactive_agent":
        agent.status = AgentStatus.DISABLED
    elif guard == "inactive_user":
        member.status = UserStatus.DISABLED
    elif guard == "disabled_resource":
        resource.sync_status = KnowledgeResourceSyncStatus.UPSTREAM_DISABLED
    elif guard == "outside_global_range":
        await session.delete(await session.scalar(select(AgentPolarRAGInstanceBinding)))
    else:
        session.add(
            AgentKnowledgePolicy(
                agent_id=agent.id,
                knowledge_resource_id=resource.id,
                select_all=True,
                excluded_subjects_json=json.dumps([["USER", member.id]]),
            )
        )
    await session.commit()
    assert not (await page(seeded)).items


async def test_admin_auth_and_invalid_origin(client):
    http, admin, member = client
    path = "/api/admin/identity-sources/missing/agents/missing/users/missing/knowledge-resources"
    assert (await http.get(path + "?access_origin=ALL", headers=member)).status_code == 403
    assert (await http.get(path + "?access_origin=invalid", headers=admin)).status_code == 422
    assert (await http.get(path + "?access_origin=FEISHU", headers=admin)).status_code == 422


async def test_all_includes_manual_group_inheritance(seeded):
    from server.models import AgentGroupAssignment, EnterpriseDirectoryGroup, EnterpriseDirectoryMembership

    session, source, agent, member, resource = seeded
    group = AgentGroupAssignment.for_identity_source_group(
        agent_id=agent.id, identity_source_id=source.id, external_group_id="group", created_by_user_id=member.id
    )
    scope = AgentKnowledgeScope(
        agent_id=agent.id,
        name="group grant",
        origin=AgentKnowledgeScopeOrigin.MANUAL,
        knowledge_resource_ids_json=json.dumps([resource.id]),
    )
    session.add_all(
        [
            group,
            scope,
            EnterpriseDirectoryGroup(
                identity_source_id=source.id,
                external_group_id="group",
                display_name="Group",
                principal_type="group",
                status="active",
            ),
            EnterpriseDirectoryMembership(
                identity_source_id=source.id,
                external_group_id="group",
                member_type="user",
                external_member_id="ou-member",
            ),
        ]
    )
    await session.flush()
    session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, group_assignment_id=group.id))
    await session.commit()
    assert [x.knowledge_resource_id for x in (await page(seeded)).items] == [resource.id]
    assert not (await page(seeded, "EXTERNAL_SYNC")).items


async def test_all_merges_external_grants_from_other_source(seeded):
    session, source, agent, member, resource = seeded
    scope = AgentKnowledgeScope(
        agent_id=agent.id,
        name="other source",
        origin=AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
        identity_source_id="other-source",
        knowledge_resource_ids_json=json.dumps([resource.id]),
    )
    session.add(scope)
    await session.flush()
    session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, user_id=member.id))
    await session.commit()
    assert [x.knowledge_resource_id for x in (await page(seeded)).items] == [resource.id]
    assert not (await page(seeded, "EXTERNAL_SYNC")).items


def test_admin_query_default_contract():
    import inspect

    parameter = inspect.signature(list_external_user_knowledge_resources).parameters["access_origin"].default
    assert parameter.default == "ALL"


async def test_manual_does_not_subtract_overlapping_external_grants(seeded):
    session, source, agent, member, resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    session.add(
        AgentKnowledgePolicy(
            agent_id=agent.id, knowledge_resource_id=resource.id, select_all=True, excluded_subjects_json="[]"
        )
    )
    await session.commit()
    assert (await page(seeded, "ALL")).items and (await page(seeded, "EXTERNAL_SYNC")).items
    assert (await page(seeded, "MANUAL")).items
    policy = await session.get(AgentKnowledgePolicy, (agent.id, resource.id))
    policy.excluded_subjects_json = json.dumps([["USER", member.id]])
    await session.commit()
    assert not (await page(seeded, "MANUAL")).items
    assert (await page(seeded, "ALL")).items


@pytest.mark.parametrize("guard", ["inactive_agent", "inactive_user", "no_agent_access"])
async def test_external_sync_is_effective_access_not_stale_authorization(seeded, guard):
    session, source, agent, member, resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    if guard == "inactive_agent":
        agent.status = AgentStatus.DISABLED
    elif guard == "inactive_user":
        member.status = UserStatus.DISABLED
    else:
        await session.delete(await session.scalar(select(AgentUserAssignment)))
    await session.commit()
    assert not (await page(seeded, "EXTERNAL_SYNC")).items


async def test_external_sync_respects_global_exclusions(seeded, monkeypatch):
    from server.api import external_knowledge_scopes as api
    from server.polarrag.access import KnowledgeResourceScope

    session, source, agent, member, resource = seeded
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()

    async def excluded(*args):
        return KnowledgeResourceScope(
            {resource.polarrag_instance_id: None}, excluded_resource_ids=frozenset({resource.id})
        )

    monkeypatch.setattr(api, "resolve_agent_global_resource_scope", excluded)
    assert not (await page(seeded, "EXTERNAL_SYNC")).items
