from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from server.api.external_knowledge_scopes import KnowledgePermissionRefreshRequest, refresh_agent_knowledge_permissions
from server.enterprise_identity import knowledge_refresh as refresh
from server.models import KnowledgeResource, SystemConfig, KnowledgeResourceSyncStatus
from tests.test_knowledge_permission_refresh import prepare, client_for, result
from tests.test_external_knowledge_scope_sync import _snapshot

pytest_plugins = ("tests._admin_api_fixtures", "tests.test_external_knowledge_scope_sync")


@pytest.mark.parametrize("body", [
    {"kb_ids": []}, {"kb_ids": [""]}, {"kb_ids": [" "]},
    {"space_id": "pks-test"}, {"polarrag_instance_id": "instance"},
    {"knowledge_resource_ids": ["resource"], "space_id": "pks-test"},
    {"knowledge_resource_ids": ["resource"], "kb_ids": ["pkb-test"]},
    {"kb_ids": ["pkb-test"], "space_id": " "},
    {"kb_ids": ["pkb-test"], "polarrag_instance_id": " "},
    {"kb_ids": ["pkb-test"] * 501},
])
def test_invalid_selector_combinations_rejected(body):
    with pytest.raises(ValidationError):
        KnowledgePermissionRefreshRequest(**body)


@pytest.mark.parametrize("selector", ["none", "space", "instance", "both"])
async def test_pkb_resolution_and_deduplication(seeded, selector):
    session, source, agent, member, resource, factory = await prepare(seeded)
    kwargs = {}
    if selector in {"space", "both"}:
        kwargs["space_id"] = resource.space_id
    if selector in {"instance", "both"}:
        kwargs["polarrag_instance_id"] = resource.polarrag_instance_id
    job = await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id] * 2, **kwargs)
    assert job["knowledge_resource_ids"] == [resource.id]
    assert job["skip_untracked_missing"] is True
    await session.commit()
    row = await session.get(SystemConfig, refresh.PREFIX + job["job_id"])
    assert json.loads(row.config_value)["targets"][0]["polarrag_instance_id"] == resource.polarrag_instance_id
    await refresh.run(job["job_id"], session_factory=factory, client_factory=client_for([_snapshot()]))
    assert (await result(session, agent, job["job_id"]))["state"] == "SUCCEEDED"


@pytest.mark.parametrize("failure", ["missing", "disabled", "inactive", "outside", "wrong_space", "wrong_instance"])
async def test_invalid_pkb_does_not_start_partial_job(seeded, failure):
    from server.models import AgentPolarRAGInstanceBinding
    session, source, agent, member, resource, factory = await prepare(seeded)
    kwargs = {}
    ids = [resource.kb_id]
    if failure == "missing":
        ids.append("missing")
    elif failure == "disabled":
        resource.enabled = False
    elif failure == "inactive":
        resource.sync_status = KnowledgeResourceSyncStatus.UPSTREAM_DISABLED
    elif failure == "outside":
        await session.delete(await session.scalar(select(AgentPolarRAGInstanceBinding)))
    elif failure == "wrong_space":
        kwargs["space_id"] = "wrong"
    else:
        kwargs["polarrag_instance_id"] = "wrong"
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await refresh.enqueue(session, agent.id, member.id, kb_ids=ids, **kwargs)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "KNOWLEDGE_RESOURCE_NOT_FOUND"
    assert exc.value.detail["space_id"] == kwargs.get("space_id")
    assert exc.value.detail["polarrag_instance_id"] == kwargs.get("polarrag_instance_id")
    assert exc.value.detail["kb_id"] == ("missing" if failure == "missing" else resource.kb_id)
    assert "applied selectors" in exc.value.detail["message"]
    assert not list(await session.scalars(select(SystemConfig).where(SystemConfig.config_key.startswith(refresh.PREFIX))))


async def test_space_selector_resolves_ambiguity_before_source_selection(seeded):
    from server.models import PolarRAGSpace
    session, source, agent, member, resource, factory = await prepare(seeded)
    space = PolarRAGSpace(
        polarrag_instance_id=resource.polarrag_instance_id, space_id="space-other", name="Other",
        identity_domain="tenant-a", enabled=True,
    )
    session.add(space)
    await session.flush()
    session.add(KnowledgeResource(
        polarrag_instance_id=resource.polarrag_instance_id, knowledge_space_id=space.knowledge_space_id,
        space_id=space.space_id, kb_id=resource.kb_id, name="Other KB", enabled=True,
        kb_type=resource.kb_type, identity_domain=resource.identity_domain,
        binding_mode=resource.binding_mode, sync_status=resource.sync_status,
    ))
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id], source_ids=[source.id])
    assert exc.value.detail["code"] == "KNOWLEDGE_RESOURCE_AMBIGUOUS"
    job = await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id], space_id=resource.space_id)
    assert job["knowledge_resource_ids"] == [resource.id]


async def test_kb_ids_bounded_wait_uses_resolved_resource(seeded, monkeypatch):
    session, source, agent, member, resource, factory = await prepare(seeded)
    monkeypatch.setattr(refresh, "start", lambda job_id: None)
    async def wait(aid, jid, timeout):
        job = await refresh.status(session, aid, jid)
        assert job["knowledge_resource_ids"] == [resource.id]
        return {**job, "state": "SUCCEEDED"}
    monkeypatch.setattr(refresh, "wait_for_result", wait)
    response = await refresh_agent_knowledge_permissions(
        agent.id, KnowledgePermissionRefreshRequest(kb_ids=[resource.kb_id] * 2, wait_timeout_seconds=10),
        BackgroundTasks(), member, session,
    )
    assert response.status_code == 200


async def test_multiple_kb_ids_disallow_wait(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    with pytest.raises(HTTPException) as exc:
        await refresh_agent_knowledge_permissions(
            agent.id, KnowledgePermissionRefreshRequest(kb_ids=[resource.kb_id, "other"], wait_timeout_seconds=10),
            BackgroundTasks(), member, session,
        )
    assert exc.value.status_code == 422


async def test_space_alone_cannot_disambiguate_across_instances(seeded):
    from server.models import PolarRAGInstance, PolarRAGSpace, PolarRAGInstanceStatus, AgentPolarRAGInstanceBinding
    session, source, agent, member, resource, factory = await prepare(seeded)
    instance = PolarRAGInstance(
        name="second", scheme="https", host="other.test", port=9200,
        username_ciphertext="u", password_ciphertext="p", status=PolarRAGInstanceStatus.ACTIVE,
        created_by=member.id,
    )
    session.add(instance)
    await session.flush()
    space = PolarRAGSpace(
        polarrag_instance_id=instance.id, space_id=resource.space_id, name="Same Space",
        identity_domain="tenant-a", enabled=True,
    )
    session.add_all([space, AgentPolarRAGInstanceBinding(
        agent_id=agent.id, polarrag_instance_id=instance.id, created_by_user_id=member.id,
    )])
    await session.flush()
    session.add(KnowledgeResource(
        knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
        space_id=space.space_id, kb_id=resource.kb_id, name="Duplicate KB",
        kb_type=resource.kb_type, identity_domain=resource.identity_domain,
        binding_mode=resource.binding_mode, sync_status=resource.sync_status, enabled=True,
    ))
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id], space_id=resource.space_id)
    assert exc.value.detail["code"] == "KNOWLEDGE_RESOURCE_AMBIGUOUS"
    job = await refresh.enqueue(
        session, agent.id, member.id, kb_ids=[resource.kb_id], space_id=resource.space_id,
        polarrag_instance_id=resource.polarrag_instance_id,
    )
    assert job["knowledge_resource_ids"] == [resource.id]


async def test_multiple_pkbs_resolve_and_publish_without_waiting(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    other = KnowledgeResource(
        knowledge_space_id=resource.knowledge_space_id, polarrag_instance_id=resource.polarrag_instance_id,
        space_id=resource.space_id, kb_id="kb-other", name="Other KB", kb_type=resource.kb_type,
        identity_domain=resource.identity_domain, binding_mode=resource.binding_mode,
        sync_status=resource.sync_status, enabled=True,
    )
    session.add(other)
    await session.commit()
    job = await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id, other.kb_id])
    assert set(job["knowledge_resource_ids"]) == {resource.id, other.id}
    assert job["skip_untracked_missing"] is True
    await session.commit()
    class Client:
        def __init__(self, **kwargs):
            pass
        async def iter_knowledge_scopes(self, **kwargs):
            kb = kwargs["kb_id"]
            yield replace(_snapshot(), snapshot_id="snapshot-"+kb, kb_id=kb, source_scope_id="scope-"+kb)
    await refresh.run(job["job_id"], session_factory=factory, client_factory=Client)
    body = await result(session, agent, job["job_id"])
    assert body["state"] == "SUCCEEDED"
    assert body["resources_completed"] == 2


async def test_saved_instance_identity_is_checked_before_publication(seeded):
    session, source, agent, member, resource, factory = await prepare(seeded)
    job = await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id])
    row = await session.get(SystemConfig, refresh.PREFIX + job["job_id"])
    value = json.loads(row.config_value)
    value["targets"][0]["polarrag_instance_id"] = "changed-instance"
    row.config_value = json.dumps(value)
    await session.commit()
    await refresh.run(job["job_id"], session_factory=factory, client_factory=client_for([_snapshot()]))
    body = await result(session, agent, job["job_id"])
    assert body["state"] == "FAILED"
    assert body["error"] == "REFRESH_RESOURCE_CHANGED"
    assert body["resources_completed"] == 0


async def test_pkb_without_selected_configured_source_identifies_failed_item(seeded):
    from server.models import EnterpriseIdentitySourceSpaceBinding
    session, source, agent, member, resource, factory = await prepare(seeded)
    binding = await session.scalar(select(EnterpriseIdentitySourceSpaceBinding))
    assert binding is not None
    await session.delete(binding)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await refresh.enqueue(session, agent.id, member.id, kb_ids=[resource.kb_id])
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "KNOWLEDGE_RESOURCE_NO_CONFIGURED_IDENTITY_SOURCE"
    assert exc.value.detail["kb_id"] == resource.kb_id
    assert exc.value.detail["space_id"] == resource.space_id
    assert exc.value.detail["polarrag_instance_id"] == resource.polarrag_instance_id
    assert not list(await session.scalars(select(SystemConfig).where(SystemConfig.config_key.startswith(refresh.PREFIX))))
    with pytest.raises(HTTPException) as internal:
        await refresh.enqueue(session, agent.id, member.id, resource_ids=[resource.id])
    assert internal.value.detail == "Knowledge resource has no selected configured identity source"
