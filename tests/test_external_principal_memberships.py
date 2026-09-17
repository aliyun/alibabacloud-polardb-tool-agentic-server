from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from server.core.crypto import encrypt
from server.models import (
    Agent,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    ExternalUserPrincipalMembership,
    IdentitySourceProvider,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    UserExternalIdentity,
)
from server.polarrag.identity import resolve_acl_context
from server.core.agent_access import list_matching_agent_group_assignment_ids


pytest_plugins = ("tests._admin_api_fixtures",)


async def test_admin_replaces_local_principal_memberships_and_acl_uses_them(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        source = EnterpriseIdentitySource.create(
            name="External chat memberships",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-local-memberships",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_synced_at = datetime.now(UTC)
        source.config_ciphertext = encrypt(
            json.dumps({"app_id": "app", "app_secret": "secret"})
        )
        instance = PolarRAGInstance(
            name="External chat memberships RAG",
            scheme="https",
            host="rag.example.test",
            port=443,
            username_ciphertext="encrypted",
            password_ciphertext="encrypted",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        agent = Agent(name="External chat Agent", created_by=admin.id)
        session.add_all([source, instance, agent])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="external-chat-space",
            name="External chat space",
            identity_domain="tenant-local-memberships",
            enabled=True,
        )
        session.add(space)
        await session.flush()
        session.add_all(
            [
                EnterpriseIdentitySourceSpaceBinding(
                    identity_source_id=source.id,
                    knowledge_space_id=space.knowledge_space_id,
                ),
                UserExternalIdentity(
                    user_id=member.id,
                    identity_provider=f"feishu:{source.tenant_id}",
                    external_subject="ou-member",
                ),
            ]
        )
        await session.commit()
        source_id = source.id
        space_id = space.knowledge_space_id
        agent_id = agent.id

    first = await http.put(
        f"/api/identity-sources/{source_id}/user-principal-memberships",
        json={
            "users": [
                {
                    "external_user_id": "ou-member",
                    "principals": [
                        {
                            "principal_type": "acl_group",
                            "principal_id": "oc-chat-1",
                        }
                    ],
                }
            ]
        },
        headers=admin_headers,
    )
    assert first.status_code == 200
    assert first.json() == {"replaced_users": 1, "membership_count": 1}
    group_assignment = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        json={
            "group_kind": "identity_source",
            "identity_source_id": source_id,
            "principal_id": "oc-chat-1",
        },
        headers=admin_headers,
    )
    assert group_assignment.status_code == 201
    assert group_assignment.json()["member_count"] == 1
    group_assignment_id = group_assignment.json()["id"]

    async with factory() as session:
        existing = (
            await session.execute(select(ExternalUserPrincipalMembership))
        ).scalar_one()
        created_at = existing.created_at
        existing.updated_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    second = await http.put(
        f"/api/identity-sources/{source_id}/user-principal-memberships",
        json={
            "users": [
                {
                    "external_user_id": "ou-member",
                    "principals": [
                        {
                            "principal_type": "acl_group",
                            "principal_id": "oc-chat-1",
                        },
                        {
                            "principal_type": "group",
                            "principal_id": "external-group-2",
                            "expires_at": (
                                datetime.now(UTC) - timedelta(seconds=1)
                            ).isoformat(),
                        },
                    ],
                },
                {"external_user_id": "ou-cleared", "principals": []},
            ]
        },
        headers=admin_headers,
    )
    assert second.status_code == 200
    assert second.json() == {"replaced_users": 2, "membership_count": 2}

    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ExternalUserPrincipalMembership).order_by(
                        ExternalUserPrincipalMembership.principal_id
                    )
                )
            ).scalars()
        )
        assert len(rows) == 2
        chat = next(row for row in rows if row.principal_id == "oc-chat-1")
        assert chat.created_at == created_at
        assert chat.updated_at is not None and chat.updated_at > chat.created_at

        context = await resolve_acl_context(session, member.id, space_id)
        assert {
            (item["provider"], item["type"], item["id"])
            for item in context["principals"]
        } >= {
            ("feishu", "user", "ou-member"),
            ("feishu", "acl_group", "oc-chat-1"),
        }
        assert all(item["id"] != "external-group-2" for item in context["principals"])
        assert await list_matching_agent_group_assignment_ids(
            session,
            agent_id,
            member.id,
        ) == {group_assignment_id}

    conflict = await http.put(
        f"/api/identity-sources/{source_id}/acl-membership-snapshot",
        json={
            "host": "meta.example.test",
            "database": "polar_rag_meta",
            "username": "readonly",
            "password": "secret",
        },
        headers=admin_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ACL_MEMBERSHIP_BACKEND_CONFLICT"
