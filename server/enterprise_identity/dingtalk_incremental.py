"""Authoritative DingTalk facts adapted to the shared directory event framework."""

from __future__ import annotations

from sqlalchemy import delete, select, update
from server.enterprise_identity.dingtalk import DingTalkDirectoryClient, DingTalkIdentityError
from server.enterprise_identity.dingtalk_auth import verified_dingtalk_config
from server.enterprise_identity.service import (
    apply_directory_event_facts,
    upsert_directory_group,
    upsert_directory_membership,
    _disable_source_owned_users,
)
from server.models import (
    IdentitySourceProvider,
    EnterpriseDirectoryUser,
    EnterpriseDirectoryGroup,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryMembershipType,
    EnterpriseIdentitySourceStatus,
)


async def invalidate_dingtalk_subject(session, source, event_type, subject_id):
    await invalidate_dingtalk_subjects(session, source, event_type, [subject_id])


async def invalidate_dingtalk_subjects(session, source, event_type, subject_ids):
    """Suspend a bounded batch with constant database round trips before ACK."""
    if event_type.startswith("user_"):
        await session.execute(
            update(EnterpriseDirectoryUser)
            .where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id.in_(subject_ids),
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
                EnterpriseDirectoryMembership.external_member_id.in_(subject_ids),
            )
        )
    else:
        await session.execute(
            update(EnterpriseDirectoryGroup)
            .where(
                EnterpriseDirectoryGroup.identity_source_id == source.id,
                EnterpriseDirectoryGroup.external_group_id.in_(subject_ids),
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
        # Keep child/member rows for the bounded refresh, but no traversal can
        # cross this inactive department until authoritative facts are applied.


async def fetch_dingtalk_directory_event(source, *, event_type, subject_id, client_factory=None):
    if source.provider != IdentitySourceProvider.DINGTALK:
        raise ValueError("INVALID_DIRECTORY_EVENT_SOURCE")
    config = verified_dingtalk_config(source)
    if subject_id is None:
        return {}  # Missing/legacy event body requests a conservative full sync.
    factory = client_factory or DingTalkDirectoryClient
    async with factory(
        tenant_id=source.tenant_id, client_id=config["client_id"], client_secret=config["client_secret"]
    ) as client:
        if event_type.startswith("user_"):
            return await client.fetch_user_event_facts(subject_id)
        return await client.fetch_department_event_facts(subject_id)


async def apply_dingtalk_directory_event(session, source, *, event_type, subject_id, data):
    if source.provider != IdentitySourceProvider.DINGTALK:
        raise ValueError("INVALID_DIRECTORY_EVENT_SOURCE")
    if subject_id is None:
        return True
    is_user = event_type.startswith("user_")
    canonical = ("contact.user." if is_user else "contact.department.") + (
        "deleted_v3" if data.get("deleted") else "updated_v3"
    )
    # Refetch even deletion notifications: delayed delivery must not remove a
    # user who has already rejoined or revert a department that now exists.
    if not data.get("deleted"):
        kind = "user" if is_user else "department"
        if str(data.get(kind, {}).get("id")) != subject_id:
            raise DingTalkIdentityError("EVENT_SUBJECT_MISMATCH")
    from datetime import UTC, datetime

    # Path facts must never reactivate another department with unresolved events.
    safe_groups = []
    for group in data.get("groups", []):
        pending = await session.scalar(
            select(EnterpriseIdentitySourceEvent.id)
            .where(
                EnterpriseIdentitySourceEvent.identity_source_id == source.id,
                EnterpriseIdentitySourceEvent.subject_id == group["id"],
                EnterpriseIdentitySourceEvent.event_type.like("org_dept_%"),
                EnterpriseIdentitySourceEvent.status != EnterpriseIdentitySourceEventStatus.PROCESSED,
            )
            .limit(1)
        )
        if pending and not (not is_user and group["id"] == subject_id):
            continue
        safe_groups.append(group)
        await upsert_directory_group(
            session,
            source,
            external_group_id=group["id"],
            display_name=group["display_name"],
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
        )
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.GROUP,
                EnterpriseDirectoryMembership.external_member_id == group["id"],
            )
        )
        if group.get("parent_id"):
            await upsert_directory_membership(
                session,
                source,
                external_group_id=group["parent_id"],
                member_type=EnterpriseDirectoryMembershipType.GROUP,
                external_member_id=group["id"],
            )
    data = {**data, "groups": safe_groups}
    for user_id in data.get("disabled_users", []):
        await invalidate_dingtalk_subject(session, source, "user_modify_org", user_id)
        await _disable_source_owned_users(session, source, external_user_id=user_id)
    request_full = await apply_directory_event_facts(
        session,
        source,
        provider=IdentitySourceProvider.DINGTALK,
        event_type=canonical,
        subject_id=subject_id,
        data=data,
    )
    if not is_user and data.get("deleted"):
        # A deleted/out-of-scope department may hide an entire authorized subtree.
        source.last_synced_at = None
        source.status = EnterpriseIdentitySourceStatus.STALE
        source.full_sync_requested_at = datetime.now(UTC)
        return True
    return request_full
