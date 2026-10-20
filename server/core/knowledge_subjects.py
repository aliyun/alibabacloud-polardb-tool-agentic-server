"""Canonical subjects used by KB select-all exclusions, independent of Agent membership."""

from sqlalchemy import false, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from server.enterprise_identity.principals import resolve_external_user_principals
from server.enterprise_identity.service import identity_provider_key, identity_source_snapshot_is_usable
from server.models import (
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    ExternalUserPrincipalMembership,
    User,
    UserDepartment,
    UserExternalIdentity,
)
from server.models.base import utc_now

SubjectKey = tuple[str, ...]
_ANCESTOR_BATCH_SIZE = 250


async def knowledge_subject_members(session: AsyncSession, key: SubjectKey):
    """Return a SQL membership query; never enumerate directory users in memory."""
    if key[0] == "USER":
        return select(User.id).where(User.id == key[1])
    if key[0] == "DEPARTMENT":
        return select(UserDepartment.user_id).where(UserDepartment.department_id == key[1])
    source = await session.get(EnterpriseIdentitySource, key[1])
    if source is None or not identity_source_snapshot_is_usable(source):
        return select(User.id).where(false())
    active_principal = select(ExternalUserPrincipalMembership.id).where(
        false() if source.provider.value == "dingtalk" else True,
        ExternalUserPrincipalMembership.identity_source_id == source.id,
        ExternalUserPrincipalMembership.external_user_id == UserExternalIdentity.external_subject,
        or_(ExternalUserPrincipalMembership.expires_at.is_(None), ExternalUserPrincipalMembership.expires_at > utc_now()),
    )
    active_directory = select(EnterpriseDirectoryUser.id).where(
        EnterpriseDirectoryUser.identity_source_id == source.id,
        EnterpriseDirectoryUser.external_user_id == UserExternalIdentity.external_subject,
        EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
    )
    query = select(UserExternalIdentity.user_id).where(
        UserExternalIdentity.identity_provider == identity_provider_key(source),
        or_(active_directory.exists(), active_principal.exists()),
    )
    if key[0] == "IDENTITY_SOURCE":
        return query
    groups = {key[2]}
    frontier = groups.copy()
    while frontier:
        children = set(await session.scalars(
            select(EnterpriseDirectoryMembership.external_member_id)
            .join(EnterpriseDirectoryGroup, (
                EnterpriseDirectoryGroup.identity_source_id == EnterpriseDirectoryMembership.identity_source_id
            ) & (
                EnterpriseDirectoryGroup.external_group_id == EnterpriseDirectoryMembership.external_member_id
            ))
            .where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.external_group_id.in_(frontier),
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.GROUP,
                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            )
        )) - groups
        groups.update(children)
        frontier = children
    directory_membership = select(EnterpriseDirectoryMembership.id).where(
        EnterpriseDirectoryMembership.identity_source_id == source.id,
        EnterpriseDirectoryMembership.external_group_id.in_(groups),
        EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
        EnterpriseDirectoryMembership.external_member_id == UserExternalIdentity.external_subject,
    )
    directory_membership = directory_membership.where(select(EnterpriseDirectoryGroup.id).where(
        EnterpriseDirectoryGroup.identity_source_id == source.id,
        EnterpriseDirectoryGroup.external_group_id == key[2],
        EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
    ).exists())
    return query.where(or_(
        directory_membership.exists(),
        active_principal.where(ExternalUserPrincipalMembership.principal_id == key[2]).exists(),
    ))


async def batch_user_knowledge_subjects(session: AsyncSession, user_ids: list[str]) -> dict[str, set[SubjectKey]]:
    """Resolve the same keys as user_knowledge_subjects with queries per source/depth, not per user."""
    result: dict[str, set[SubjectKey]] = {uid: {("USER", uid)} for uid in user_ids}
    for uid, department_id in await session.execute(select(UserDepartment.user_id, UserDepartment.department_id).where(
        UserDepartment.user_id.in_(user_ids),
    )):
        result[uid].add(("DEPARTMENT", department_id))
    identities: dict[str, dict[str, str]] = {}
    for uid, provider, external_id in await session.execute(select(
        UserExternalIdentity.user_id, UserExternalIdentity.identity_provider, UserExternalIdentity.external_subject,
    ).where(UserExternalIdentity.user_id.in_(user_ids))):
        identities.setdefault(uid, {})[provider] = external_id
    if not identities:
        return result
    for source in await session.scalars(select(EnterpriseIdentitySource)):
        if not identity_source_snapshot_is_usable(source):
            continue
        provider = identity_provider_key(source)
        users = {uid: ids[provider] for uid, ids in identities.items() if provider in ids}
        if not users:
            continue
        external_ids = set(users.values())
        active = set(await session.scalars(select(EnterpriseDirectoryUser.external_user_id).where(
            EnterpriseDirectoryUser.identity_source_id == source.id,
            EnterpriseDirectoryUser.external_user_id.in_(external_ids),
            EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
        )))
        local: dict[str, set[str]] = {}
        for external_id, principal in await session.execute(select(
            ExternalUserPrincipalMembership.external_user_id, ExternalUserPrincipalMembership.principal_id,
        ).where(
            ExternalUserPrincipalMembership.identity_source_id == source.id,
            false() if source.provider.value == "dingtalk" else True,
            ExternalUserPrincipalMembership.external_user_id.in_(external_ids),
            or_(ExternalUserPrincipalMembership.expires_at.is_(None),
                ExternalUserPrincipalMembership.expires_at > utc_now()),
        )):
            local.setdefault(external_id, set()).add(str(principal))
        frontier = {uid: {eid} for uid, eid in users.items() if eid in active or eid in local}
        for uid in frontier:
            result[uid].add(("IDENTITY_SOURCE", source.id))
            result[uid].update(("GROUP", source.id, group) for group in local.get(users[uid], set()))
        visited: dict[str, set[str]] = {uid: set() for uid in frontier}
        member_type = EnterpriseDirectoryMembershipType.USER
        while any(frontier.values()):
            parents: dict[str, set[str]] = {}
            for member, group in await session.execute(select(
                EnterpriseDirectoryMembership.external_member_id, EnterpriseDirectoryGroup.external_group_id,
            ).join(EnterpriseDirectoryMembership, (
                EnterpriseDirectoryMembership.identity_source_id == EnterpriseDirectoryGroup.identity_source_id
            ) & (EnterpriseDirectoryMembership.external_group_id == EnterpriseDirectoryGroup.external_group_id)).where(
                EnterpriseDirectoryGroup.identity_source_id == source.id,
                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                EnterpriseDirectoryMembership.member_type == member_type,
                EnterpriseDirectoryMembership.external_member_id.in_(set().union(*frontier.values())),
            )):
                parents.setdefault(member, set()).add(group)
            for uid, members in frontier.items():
                groups = set().union(*(parents.get(member, set()) for member in members)) - visited[uid]
                result[uid].update(("GROUP", source.id, group) for group in groups)
                visited[uid].update(groups)
                frontier[uid] = groups
            member_type = EnterpriseDirectoryMembershipType.GROUP
    return result


async def group_knowledge_subjects(session: AsyncSession, source_id: str, group_id: str) -> set[SubjectKey]:
    result = {("IDENTITY_SOURCE", source_id), ("GROUP", source_id, group_id)}
    frontier = {group_id}
    visited = {group_id}
    while frontier:
        parents = set(await session.scalars(
            select(EnterpriseDirectoryMembership.external_group_id)
            .join(EnterpriseDirectoryGroup, (
                EnterpriseDirectoryGroup.identity_source_id == EnterpriseDirectoryMembership.identity_source_id
            ) & (
                EnterpriseDirectoryGroup.external_group_id == EnterpriseDirectoryMembership.external_group_id
            ))
            .where(
                EnterpriseDirectoryMembership.identity_source_id == source_id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.GROUP,
                EnterpriseDirectoryMembership.external_member_id.in_(frontier),
                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            )
        )) - visited
        result.update(("GROUP", source_id, parent) for parent in parents)
        visited.update(parents)
        frontier = parents
    return result


async def batch_group_knowledge_subjects(
    session: AsyncSession, groups: set[tuple[str, str]],
) -> dict[SubjectKey, set[SubjectKey]]:
    """Read shared ancestor edges once per frontier, then expand each group's keys."""
    parents: dict[tuple[str, str], set[tuple[str, str]]] = {}
    frontier = groups.copy()
    visited: set[tuple[str, str]] = set()
    while frontier:
        ordered = sorted(frontier)
        for start in range(0, len(ordered), _ANCESTOR_BATCH_SIZE):
            rows = await session.execute(select(
                EnterpriseDirectoryMembership.identity_source_id,
                EnterpriseDirectoryMembership.external_member_id,
                EnterpriseDirectoryMembership.external_group_id,
            ).join(EnterpriseDirectoryGroup, (
                EnterpriseDirectoryGroup.identity_source_id == EnterpriseDirectoryMembership.identity_source_id
            ) & (
                EnterpriseDirectoryGroup.external_group_id == EnterpriseDirectoryMembership.external_group_id
            )).where(
                tuple_(EnterpriseDirectoryMembership.identity_source_id,
                       EnterpriseDirectoryMembership.external_member_id).in_(ordered[start:start + _ANCESTOR_BATCH_SIZE]),
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.GROUP,
                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            ))
            for source_id, child, parent in rows:
                parents.setdefault((source_id, child), set()).add((source_id, parent))
        visited.update(frontier)
        frontier = set().union(*(parents.get(key, set()) for key in frontier)) - visited

    result: dict[SubjectKey, set[SubjectKey]] = {}
    for source_id, group_id in groups:
        ancestors = {(source_id, group_id)}
        frontier = ancestors.copy()
        while frontier:
            frontier = set().union(*(parents.get(key, set()) for key in frontier)) - ancestors
            ancestors.update(frontier)
        result[("GROUP", source_id, group_id)] = {
            ("IDENTITY_SOURCE", source_id),
            *(("GROUP", source, ancestor) for source, ancestor in ancestors),
        }
    return result


async def user_knowledge_subjects(session: AsyncSession, user_id: str) -> set[SubjectKey]:
    result: set[SubjectKey] = {("USER", user_id)}
    result.update(
        ("DEPARTMENT", department_id)
        for department_id in await session.scalars(
            select(UserDepartment.department_id).where(UserDepartment.user_id == user_id)
        )
    )
    identities = dict((await session.execute(
        select(UserExternalIdentity.identity_provider, UserExternalIdentity.external_subject)
        .where(UserExternalIdentity.user_id == user_id)
    )).tuples().all())
    if not identities:
        return result
    for source in await session.scalars(select(EnterpriseIdentitySource)):
        external_id = identities.get(identity_provider_key(source))
        if external_id is None or not identity_source_snapshot_is_usable(source):
            continue
        principals = await resolve_external_user_principals(session, source.id, external_id)
        directory_user = await session.scalar(
            select(EnterpriseDirectoryUser.id).where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == external_id,
                EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            ).limit(1)
        )
        if not directory_user and not principals:
            continue
        result.add(("IDENTITY_SOURCE", source.id))
        result.update(("GROUP", source.id, principal_id) for _, principal_id in principals)
        frontier = {external_id}
        visited: set[str] = set()
        member_type = EnterpriseDirectoryMembershipType.USER
        while frontier:
            groups = set(await session.scalars(
                select(EnterpriseDirectoryGroup.external_group_id)
                .join(EnterpriseDirectoryMembership, (
                    EnterpriseDirectoryMembership.identity_source_id == EnterpriseDirectoryGroup.identity_source_id
                ) & (
                    EnterpriseDirectoryMembership.external_group_id == EnterpriseDirectoryGroup.external_group_id
                ))
                .where(
                    EnterpriseDirectoryGroup.identity_source_id == source.id,
                    EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                    EnterpriseDirectoryMembership.member_type == member_type,
                    EnterpriseDirectoryMembership.external_member_id.in_(frontier),
                )
            )) - visited
            result.update(("GROUP", source.id, group_id) for group_id in groups)
            visited.update(groups)
            frontier = groups
            member_type = EnterpriseDirectoryMembershipType.GROUP
    return result
