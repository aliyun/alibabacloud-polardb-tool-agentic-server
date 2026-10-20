from datetime import UTC, datetime, timedelta
from dataclasses import replace
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from tests.test_external_knowledge_scope_sync import seeded as source_fixture, _snapshot
from server.core.crypto import encrypt
from server.core.external_knowledge_scope_sync import (
    import_external_knowledge_scope_snapshot,
    ImportedGrant,
    ExternalKnowledgeScopeSyncError,
)
from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope
from server.enterprise_identity.dingtalk_auth import dingtalk_config_digest
from server.enterprise_identity.service import identity_source_snapshot_is_usable
from server.models import (
    EnterpriseDirectoryUser,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryEntryStatus,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    UserExternalIdentity,
)


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    import base64
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"t" * 32).decode())


seeded = source_fixture


async def ding_source(seeded):
    session, source, agent, member, resource = seeded
    source.provider = IdentitySourceProvider.DINGTALK
    source.permission_error = None
    source.permission_observed_at = datetime.now(UTC)
    source.last_synced_at = datetime.now(UTC)
    cfg = {"client_id": "app", "client_secret": "secret"}
    cfg["tenant_verified_digest"] = dingtalk_config_digest(source, cfg)
    source.config_ciphertext = encrypt(json.dumps(cfg))
    mapping = await session.scalar(select(UserExternalIdentity))
    mapping.identity_provider = "dingtalk:tenant-a"
    session.add(
        EnterpriseDirectoryUser(
            identity_source_id=source.id,
            external_user_id="ou-member",
            display_name="Member",
            status=EnterpriseDirectoryEntryStatus.ACTIVE,
        )
    )
    session.add(
        EnterpriseDirectoryGroup(
            identity_source_id=source.id,
            external_group_id="2",
            display_name="Department",
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
        )
    )
    session.add(
        EnterpriseDirectoryMembership(
            identity_source_id=source.id,
            external_group_id="2",
            member_type=EnterpriseDirectoryMembershipType.USER,
            external_member_id="ou-member",
        )
    )
    await session.commit()
    return session, source, agent, member, resource


@pytest.mark.parametrize("kind,subject", [("user", "ou-member"), ("department", "2"), ("domain", "tenant-a")])
async def test_native_grants_allow_and_revoke_with_agent_ceiling(seeded, kind, subject):
    session, source, agent, member, resource = await ding_source(seeded)
    snapshot = replace(
        _snapshot(),
        source_scope_type="document_read_discovery",
        source_scope_id="kb-a",
        grants=(ImportedGrant(kind, subject),),
    )
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=snapshot)
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert scope.allows(resource)
    await import_external_knowledge_scope_snapshot(
        session,
        identity_source_id=source.id,
        snapshot=replace(
            snapshot,
            generation="revoked",
            fingerprint="revoked",
            grants=(),
            source_updated_at=datetime(2026, 10, 14, tzinfo=UTC),
        ),
    )
    await session.commit()
    scope = await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)
    assert not scope.allows(resource)


@pytest.mark.parametrize("kind,subject", [("domain", "another-corp"), ("group", "tag"), ("chat", "conversation")])
async def test_cross_tenant_and_unsupported_grants_rejected(seeded, kind, subject):
    session, source, *_ = await ding_source(seeded)
    with pytest.raises(ExternalKnowledgeScopeSyncError):
        await import_external_knowledge_scope_snapshot(
            session, identity_source_id=source.id, snapshot=replace(_snapshot(), grants=(ImportedGrant(kind, subject),))
        )


async def test_stream_event_is_tenant_fenced_deduplicated_and_revokes_immediately(seeded):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from server.enterprise_identity.dingtalk_events import ingest_dingtalk_event

    session, source, *_ = await ding_source(seeded)
    source.event_listener_worker_id = "worker"
    source.event_listener_lease_until = datetime.now(UTC) + timedelta(seconds=30)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    digest = dingtalk_config_digest(
        source, json.loads(__import__("server.core.crypto", fromlist=["decrypt"]).decrypt(source.config_ciphertext))
    )
    arguments = dict(event_id="event", event_type="user_leave_org", corp_id="tenant-a")
    assert await ingest_dingtalk_event(factory, source.id, "worker", digest, **arguments)
    assert not await ingest_dingtalk_event(factory, source.id, "worker", digest, **arguments)
    await session.refresh(source)
    assert source.last_synced_at is None and not identity_source_snapshot_is_usable(source)
    with pytest.raises(ValueError):
        await ingest_dingtalk_event(
            factory, source.id, "worker", digest, **{**arguments, "event_id": "evil", "corp_id": "other"}
        )


def test_directory_freshness_is_bounded_even_when_status_active():
    source = SimpleNamespace(
        provider=IdentitySourceProvider.DINGTALK,
        status=EnterpriseIdentitySourceStatus.ACTIVE,
        permission_error=None,
        last_synced_at=datetime.now(UTC) - timedelta(seconds=901),
        stale_after_seconds=86400,
    )
    assert not identity_source_snapshot_is_usable(source)


async def test_existing_user_bindings_stop_granting_when_source_observation_expires(seeded):
    session, source, agent, member, resource = await ding_source(seeded)
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    assert (await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)).allows(resource)
    source.permission_observed_at = datetime.now(UTC) - timedelta(seconds=901)
    await session.commit()
    assert not (await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)).allows(resource)


async def test_disabled_directory_users_do_not_gain_discovery_from_direct_grants(seeded):
    session, source, agent, member, resource = await ding_source(seeded)
    directory = await session.scalar(select(EnterpriseDirectoryUser))
    directory.status = EnterpriseDirectoryEntryStatus.DISABLED
    await session.commit()
    await import_external_knowledge_scope_snapshot(session, identity_source_id=source.id, snapshot=_snapshot())
    await session.commit()
    assert not (await resolve_agent_knowledge_resource_scope(session, agent.id, member.id)).allows(resource)
