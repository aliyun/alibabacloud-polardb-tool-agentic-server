from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core import agent_enterprise_access_service
from server.enterprise_identity.service import identity_provider_key
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AuditLog,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    UserExternalIdentity,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@dataclass(frozen=True)
class SeededEnterpriseAccess:
    agent_id: str
    source_id: str
    other_source_id: str
    other_source_group_id: str
    other_source_external_group_id: str
    group_id: str
    user_id: str
    unmapped_user_id: str
    space_id: str
    unbound_space_id: str


async def _seed_enterprise_access(setup) -> SeededEnterpriseAccess:
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="enterprise-access-agent", created_by=admin.id)
        bound_instance = PolarRAGInstance(
            name="Bound PolarRAG",
            scheme="https",
            host="bound-rag.example.test",
            port=443,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        unbound_instance = PolarRAGInstance(
            name="Unbound PolarRAG",
            scheme="https",
            host="unbound-rag.example.test",
            port=443,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        source = EnterpriseIdentitySource.create(
            name="Enterprise directory",
            provider=IdentitySourceProvider.SHAREPOINT,
            tenant_id="tenant-enterprise-access",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        other_source = EnterpriseIdentitySource.create(
            name="Other enterprise directory",
            provider=IdentitySourceProvider.SHAREPOINT,
            tenant_id="tenant-enterprise-access-other",
        )
        other_source.status = EnterpriseIdentitySourceStatus.ACTIVE
        other_source.last_synced_at = datetime.now(UTC)
        session.add_all(
            [agent, bound_instance, unbound_instance, source, other_source]
        )
        await session.flush()

        group = EnterpriseDirectoryGroup(
            identity_source_id=source.id,
            external_group_id="group-engineering",
            display_name="Engineering",
            principal_type=EnterpriseDirectoryPrincipalType.GROUP,
        )
        other_group = EnterpriseDirectoryGroup(
            identity_source_id=other_source.id,
            external_group_id="group-other",
            display_name="Other group",
            principal_type=EnterpriseDirectoryPrincipalType.GROUP,
        )
        directory_user = EnterpriseDirectoryUser(
            identity_source_id=source.id,
            external_user_id="member-subject",
            display_name="Member",
        )
        bound_space = PolarRAGSpace(
            polarrag_instance_id=bound_instance.id,
            space_id="space-bound",
            name="Bound Space",
            identity_domain="domain-bound",
            enabled=True,
            last_synced_at=datetime.now(UTC),
        )
        unbound_space = PolarRAGSpace(
            polarrag_instance_id=unbound_instance.id,
            space_id="space-unbound",
            name="Unbound Space",
            identity_domain="domain-unbound",
            enabled=True,
            last_synced_at=datetime.now(UTC),
        )
        binding = AgentPolarRAGInstanceBinding(
            agent_id=agent.id,
            polarrag_instance_id=bound_instance.id,
            created_by_user_id=admin.id,
            public_knowledge_resource_ids_json='["resource-selected"]',
        )
        identity = UserExternalIdentity(
            user_id=member.id,
            identity_provider=identity_provider_key(source),
            external_subject=directory_user.external_user_id,
        )
        session.add_all(
            [
                group,
                other_group,
                directory_user,
                bound_space,
                unbound_space,
                binding,
                identity,
            ]
        )
        await session.commit()
        return SeededEnterpriseAccess(
            agent_id=agent.id,
            source_id=source.id,
            other_source_id=other_source.id,
            other_source_group_id=other_group.id,
            other_source_external_group_id=other_group.external_group_id,
            group_id=group.id,
            user_id=member.id,
            unmapped_user_id=admin.id,
            space_id=bound_space.knowledge_space_id,
            unbound_space_id=unbound_space.knowledge_space_id,
        )


async def _count_rows(setup, model) -> int:
    factory, _admin, _member = setup
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _preview(http, admin_headers, seeded, selection):
    return await http.post(
        f"/api/agents/{seeded.agent_id}/enterprise-access/preview",
        json=selection,
        headers=admin_headers,
    )


async def _apply_previewed(http, admin_headers, seeded, selection):
    preview = await _preview(http, admin_headers, seeded, selection)
    return await http.post(
        f"/api/agents/{seeded.agent_id}/enterprise-access/apply",
        json={**selection, "preview_hash": preview.json()["preview_hash"]},
        headers=admin_headers,
    )


async def _assert_relation_counts(
    setup,
    *,
    groups: int,
    users: int,
    space_bindings: int,
    audits: int | None = None,
) -> None:
    assert await _count_rows(setup, AgentGroupAssignment) == groups
    assert await _count_rows(setup, AgentUserAssignment) == users
    assert (
        await _count_rows(setup, EnterpriseIdentitySourceSpaceBinding)
        == space_bindings
    )
    if audits is not None:
        assert await _count_rows(setup, AuditLog) == audits


def _space_binding_lookup(source_id: str, space_id: str):
    return select(EnterpriseIdentitySourceSpaceBinding).where(
        EnterpriseIdentitySourceSpaceBinding.identity_source_id == source_id,
        EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == space_id,
    )


async def test_enterprise_access_preview_rejects_implicit_all_users(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)

    response = await _preview(
        http,
        admin_headers,
        seeded,
        {
            "identity_source_id": seeded.source_id,
            "directory_group_ids": [],
            "pas_user_ids": [],
            "knowledge_space_ids": [seeded.space_id],
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("selection_field", "seeded_field", "limit"),
    (
        ("directory_group_ids", "group_id", 500),
        ("pas_user_ids", "user_id", 500),
        ("knowledge_space_ids", "space_id", 200),
    ),
)
async def test_enterprise_access_preview_rejects_oversized_selection_lists(
    client, setup, selection_field, seeded_field, limit
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    selection[selection_field] = [getattr(seeded, seeded_field)] * (limit + 1)

    response = await _preview(http, admin_headers, seeded, selection)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", selection_field]
    assert detail[0]["type"] == "too_long"


@pytest.mark.parametrize(
    ("selection_field", "seeded_field", "expected_code"),
    (
        (
            "knowledge_space_ids",
            "unbound_space_id",
            "ENTERPRISE_ACCESS_SPACE_NOT_ELIGIBLE",
        ),
        (
            "directory_group_ids",
            "other_source_group_id",
            "ENTERPRISE_ACCESS_GROUP_NOT_ELIGIBLE",
        ),
        (
            "pas_user_ids",
            "unmapped_user_id",
            "ENTERPRISE_ACCESS_USER_NOT_ELIGIBLE",
        ),
    ),
)
async def test_enterprise_access_preview_rejects_ineligible_selection(
    client, setup, selection_field, seeded_field, expected_code
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    selection[selection_field] = [getattr(seeded, seeded_field)]

    response = await _preview(
        http,
        admin_headers,
        seeded,
        selection,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code


async def test_enterprise_access_preview_accepts_any_active_user_mapping(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    factory, _admin, _member = setup
    async with factory() as session:
        second_directory_user = EnterpriseDirectoryUser(
            identity_source_id=seeded.source_id,
            external_user_id="member-second-subject",
            display_name="Member second identity",
        )
        session.add(second_directory_user)
        await session.flush()
        source = await session.get(
            EnterpriseIdentitySource,
            seeded.source_id,
        )
        assert source is not None
        session.add(
            UserExternalIdentity(
                user_id=seeded.user_id,
                identity_provider=identity_provider_key(source),
                external_subject=second_directory_user.external_user_id,
            )
        )
        await session.commit()

    response = await _preview(
        http,
        admin_headers,
        seeded,
        {
            "identity_source_id": seeded.source_id,
            "pas_user_ids": [seeded.user_id],
            "knowledge_space_ids": [seeded.space_id],
        },
    )

    assert response.status_code == 200
    assert [item["relation_type"] for item in response.json()["creates"]] == [
        "agent_user"
    ]


async def test_enterprise_access_preview_is_read_only(client, setup) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)

    response = await _preview(
        http,
        admin_headers,
        seeded,
        _selection(seeded),
    )

    assert response.status_code == 200
    await _assert_relation_counts(
        setup,
        groups=0,
        users=0,
        space_bindings=0,
        audits=0,
    )


def _selection(seeded: SeededEnterpriseAccess) -> dict[str, object]:
    return {
        "identity_source_id": seeded.source_id,
        "all_synced_users": True,
        "directory_group_ids": [seeded.group_id],
        "pas_user_ids": [seeded.user_id],
        "knowledge_space_ids": [seeded.space_id],
    }


async def test_enterprise_access_apply_is_atomic_and_idempotent(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    applied = await _apply_previewed(http, admin_headers, seeded, selection)

    assert applied.status_code == 200
    assert {
        item["relation_type"] for item in applied.json()["creates"]
    } == {
        "agent_identity_source_all_users",
        "agent_identity_source_group",
        "agent_user",
    }
    assert [
        item["relation_type"] for item in applied.json()["global_changes"]
    ] == ["identity_source_space_binding"]
    await _assert_relation_counts(
        setup,
        groups=2,
        users=1,
        space_bindings=1,
    )

    repeated = await _apply_previewed(http, admin_headers, seeded, selection)

    assert repeated.status_code == 200
    assert repeated.json()["creates"] == []
    assert repeated.json()["global_changes"] == []
    assert len(repeated.json()["reuses"]) == 4
    await _assert_relation_counts(
        setup,
        groups=2,
        users=1,
        space_bindings=1,
    )
    factory, _admin, _member = setup
    async with factory() as session:
        audit_actions = list(
            (await session.execute(select(AuditLog.action))).scalars()
        )
    assert audit_actions.count("identity_source.space_bind") == 1
    assert audit_actions.count("agent_enterprise_access.configure") == 2


async def test_enterprise_access_apply_serializes_concurrent_requests(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    preview = await _preview(http, admin_headers, seeded, selection)
    body = {**selection, "preview_hash": preview.json()["preview_hash"]}

    first, second = await asyncio.gather(
        http.post(
            f"/api/agents/{seeded.agent_id}/enterprise-access/apply",
            json=body,
            headers=admin_headers,
        ),
        http.post(
            f"/api/agents/{seeded.agent_id}/enterprise-access/apply",
            json=body,
            headers=admin_headers,
        ),
    )

    responses = (first, second)
    assert sorted(response.status_code for response in responses) == [200, 409]
    succeeded = next(response for response in responses if response.status_code == 200)
    stale = next(response for response in responses if response.status_code == 409)
    assert stale.json()["detail"]["code"] == "ENTERPRISE_ACCESS_PREVIEW_STALE"
    assert len(succeeded.json()["creates"]) == 3
    assert len(succeeded.json()["global_changes"]) == 1
    await _assert_relation_counts(
        setup,
        groups=2,
        users=1,
        space_bindings=1,
        audits=2,
    )


async def test_enterprise_access_apply_rejects_a_stale_preview_without_writes(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = {
        **_selection(seeded),
        "directory_group_ids": [],
        "pas_user_ids": [],
    }
    preview = await _preview(http, admin_headers, seeded, selection)
    factory, _admin, _member = setup
    async with factory() as session:
        session.add(
            EnterpriseIdentitySourceSpaceBinding(
                identity_source_id=seeded.source_id,
                knowledge_space_id=seeded.space_id,
            )
        )
        await session.commit()

    stale = await http.post(
        f"/api/agents/{seeded.agent_id}/enterprise-access/apply",
        json={**selection, "preview_hash": preview.json()["preview_hash"]},
        headers=admin_headers,
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == (
        "ENTERPRISE_ACCESS_PREVIEW_STALE"
    )
    assert stale.json()["detail"]["preview"]["global_changes"] == []
    await _assert_relation_counts(
        setup,
        groups=0,
        users=0,
        space_bindings=1,
        audits=0,
    )


async def test_enterprise_access_apply_rolls_back_when_audit_fails(
    client, setup, monkeypatch
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(
        "server.core.agent_enterprise_access_service.log_audit",
        fail_audit,
        raising=False,
    )
    failed = await _apply_previewed(http, admin_headers, seeded, selection)

    assert failed.status_code == 503
    assert failed.json() == {"detail": "Audit unavailable"}
    await _assert_relation_counts(
        setup,
        groups=0,
        users=0,
        space_bindings=0,
        audits=0,
    )


async def test_deleting_agent_enterprise_grants_preserves_shared_scope(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    applied = await _apply_previewed(http, admin_headers, seeded, selection)
    assignment_ids = [
        item["relation_id"]
        for item in applied.json()["creates"]
        if item["relation_type"].startswith("agent_identity_source")
    ]
    factory, admin, _member = setup
    async with factory() as session:
        session.add(
            AgentGroupAssignment.for_identity_source_group(
                agent_id=seeded.agent_id,
                identity_source_id=seeded.other_source_id,
                external_group_id=seeded.other_source_external_group_id,
                created_by_user_id=admin.id,
            )
        )
        await session.commit()

    for assignment_id in assignment_ids:
        removed = await http.delete(
            f"/api/agents/{seeded.agent_id}/group-assignments/{assignment_id}",
            headers=admin_headers,
        )
        assert removed.status_code == 204

    async with factory() as session:
        binding = (
            await session.execute(
                select(AgentPolarRAGInstanceBinding).where(
                    AgentPolarRAGInstanceBinding.agent_id == seeded.agent_id
                )
            )
        ).scalar_one()
        assert binding.public_knowledge_resource_ids_json == (
            '["resource-selected"]'
        )
    assert await _count_rows(setup, AgentGroupAssignment) == 1
    assert await _count_rows(setup, AgentUserAssignment) == 1
    assert await _count_rows(setup, EnterpriseIdentitySourceSpaceBinding) == 1
    assert await _count_rows(setup, AgentPolarRAGInstanceBinding) == 1


async def test_exact_duplicate_insert_is_reused(setup) -> None:
    seeded = await _seed_enterprise_access(setup)
    factory, _admin, _member = setup
    async with factory() as session:
        existing = EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=seeded.source_id,
            knowledge_space_id=seeded.space_id,
        )
        session.add(existing)
        await session.commit()
        row, created = await agent_enterprise_access_service._insert_or_reuse(
            session,
            EnterpriseIdentitySourceSpaceBinding(
                identity_source_id=seeded.source_id,
                knowledge_space_id=seeded.space_id,
            ),
            lookup=_space_binding_lookup(seeded.source_id, seeded.space_id),
            constraint_name="uq_identity_source_space_binding",
            sqlite_columns=(
                "enterprise_identity_source_space_bindings.identity_source_id, "
                "enterprise_identity_source_space_bindings.knowledge_space_id"
            ),
        )

        assert created is False
        assert row.id == existing.id


def test_mysql_duplicate_for_expected_constraint_is_reused() -> None:
    from asyncmy.errors import IntegrityError as AsyncmyIntegrityError

    exc = IntegrityError(
        "INSERT",
        {},
        AsyncmyIntegrityError(
            1062,
            "Duplicate entry 'x' for key 'uq_identity_source_space_binding'",
        ),
    )

    assert agent_enterprise_access_service._is_exact_duplicate(
        exc,
        constraint_name="uq_identity_source_space_binding",
        sqlite_columns="unused",
    )


def test_mysql_duplicate_for_other_constraint_is_not_reused() -> None:
    from asyncmy.errors import IntegrityError as AsyncmyIntegrityError

    exc = IntegrityError(
        "INSERT",
        {},
        AsyncmyIntegrityError(
            1062,
            "Duplicate entry 'x' for key 'uq_other_unique_relation'",
        ),
    )

    assert not agent_enterprise_access_service._is_exact_duplicate(
        exc,
        constraint_name="uq_identity_source_space_binding",
        sqlite_columns="unused",
    )


async def test_unrelated_integrity_error_is_not_treated_as_reuse(setup) -> None:
    factory, _admin, _member = setup
    async with factory() as session:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await agent_enterprise_access_service._insert_or_reuse(
                session,
                EnterpriseIdentitySourceSpaceBinding(
                    identity_source_id="missing-source",
                    knowledge_space_id="missing-space",
                ),
                lookup=_space_binding_lookup("missing-source", "missing-space"),
                constraint_name="uq_identity_source_space_binding",
                sqlite_columns=(
                    "enterprise_identity_source_space_bindings.identity_source_id, "
                    "enterprise_identity_source_space_bindings.knowledge_space_id"
                ),
            )


async def test_enterprise_access_apply_rolls_back_when_commit_fails(
    client, setup, monkeypatch
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    failed = await _apply_previewed(http, admin_headers, seeded, selection)

    assert failed.status_code == 503
    assert failed.json() == {
        "detail": "Enterprise access configuration unavailable"
    }
    await _assert_relation_counts(
        setup,
        groups=0,
        users=0,
        space_bindings=0,
        audits=0,
    )


async def test_enterprise_access_apply_rolls_back_unrelated_integrity_error(
    client, setup, monkeypatch
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)
    selection = _selection(seeded)
    original_insert = agent_enterprise_access_service._insert_or_reuse
    call_count = 0

    async def fail_second_insert(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise IntegrityError(
                "INSERT",
                {},
                RuntimeError("unrelated integrity failure"),
            )
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(
        agent_enterprise_access_service,
        "_insert_or_reuse",
        fail_second_insert,
    )
    failed = await _apply_previewed(http, admin_headers, seeded, selection)

    assert failed.status_code == 409
    assert failed.json()["detail"]["code"] == "ENTERPRISE_ACCESS_CONFLICT"
    await _assert_relation_counts(
        setup,
        groups=0,
        users=0,
        space_bindings=0,
        audits=0,
    )


async def test_enterprise_access_request_forbids_client_identity_context(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    seeded = await _seed_enterprise_access(setup)

    response = await _preview(
        http,
        admin_headers,
        seeded,
        {
            **_selection(seeded),
            "provider": "sharepoint",
            "principal": "forged-principal",
            "identity_domain": "forged-domain",
            "acl_context": {"principals": []},
        },
    )

    assert response.status_code == 422
    await _assert_relation_counts(
        setup,
        groups=0,
        users=0,
        space_bindings=0,
    )
