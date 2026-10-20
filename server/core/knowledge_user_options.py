"""Read-only, set-based candidate filtering for the KB user-binding picker."""

from typing import Any

from sqlalchemy import and_, false, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.agent_knowledge_scope import excluded_subjects, knowledge_policies, resource_ids_from_json
from server.core.knowledge_subjects import batch_user_knowledge_subjects, knowledge_subject_members
from server.core.user_option_availability import user_option_availability
from server.models import (
    Agent, AgentGroupAssignment, AgentGroupKind, AgentKnowledgeScopeMode, AgentKnowledgeScopeOrigin,
    AgentUserAssignment, AuthProvider, EnterprisePrincipalAssignment, EnterprisePrincipalStatus,
    EnterprisePrincipalType, User, UserDepartment, UserStatus,
)
from server.models.base import utc_now


async def _group_members(session: AsyncSession, agent_id: str):
    queries = []
    for assignment in await session.scalars(select(AgentGroupAssignment).where(
        AgentGroupAssignment.agent_id == agent_id,
    )):
        if assignment.group_kind == AgentGroupKind.DEPARTMENT:
            members = select(UserDepartment.user_id).where(
                UserDepartment.department_id == assignment.department_id,
            )
        elif assignment.group_kind == AgentGroupKind.ENTERPRISE:
            members = select(EnterprisePrincipalAssignment.pas_user_id).where(
                EnterprisePrincipalAssignment.identity_domain == assignment.identity_domain,
                EnterprisePrincipalAssignment.provider == assignment.provider,
                EnterprisePrincipalAssignment.principal_id == assignment.principal_id,
                EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.GROUP,
                EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
                or_(EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > utc_now()),
            )
        else:
            key = (("IDENTITY_SOURCE", assignment.identity_source_id or "")
                   if assignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL
                   else ("GROUP", assignment.identity_source_id or "", assignment.principal_id or ""))
            members = await knowledge_subject_members(session, key)
        member_rows = members.subquery()
        queries.append(select(member_rows.c[0].label("user_id"), literal(assignment.id).label("assignment_id")))
    if not queries:
        queries = [select(User.id.label("user_id"), literal("").label("assignment_id")).where(false())]
    return union_all(*queries).cte("picker_group_members")


async def user_options(
    session: AsyncSession, agent: Agent, resource_ids: list[str], grants: list[Any],
    *, search: str | None, binding_status: str, offset: int, limit: int,
) -> tuple[list[dict[str, Any]], int | None]:
    groups = await _group_members(session, agent.id)
    member = or_(
        User.id.in_(select(AgentUserAssignment.user_id).where(
            AgentUserAssignment.agent_id == agent.id, AgentUserAssignment.is_direct.is_(True),
        )),
        User.id.in_(select(groups.c.user_id)),
        and_(literal(agent.all_builtin_users), User.auth_provider == AuthProvider.BUILTIN,
             User.status == UserStatus.ACTIVE),
    )
    policies = await knowledge_policies(session, agent.id)
    legacy_all = agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL
    selected = {rid: policies[rid].select_all if rid in policies else legacy_all for rid in resource_ids}
    exclusions = {rid: excluded_subjects(policies[rid]) if rid in policies else set() for rid in resource_ids}
    grant_resources = [(grant, resource_ids_from_json(grant.knowledge_resource_ids_json)) for grant in grants]
    user_grants: dict[str, list[Any]] = {}
    group_grants: dict[str, list[Any]] = {}
    for grant, resources in grant_resources:
        if grant.user_id:
            user_grants.setdefault(grant.user_id, []).append((grant, resources))
        if grant.group_assignment_id:
            group_grants.setdefault(grant.group_assignment_id, []).append((grant, resources))
    exclusion_queries = {}
    exclusion_keys = set().union(*exclusions.values())
    user_exclusions = {key[1] for key in exclusion_keys if key[0] == "USER"}
    department_exclusions = {key[1] for key in exclusion_keys if key[0] == "DEPARTMENT"}
    for key in exclusion_keys:
        if key[0] not in {"USER", "DEPARTMENT"}:
            exclusion_queries[key] = await knowledge_subject_members(session, key)
    base_bound = []
    for rid in resource_ids:
        direct_ids = {grant.user_id for grant, ids in grant_resources if rid in ids and grant.user_id}
        group_ids = {grant.group_assignment_id for grant, ids in grant_resources
                     if rid in ids and grant.group_assignment_id}
        base_bound.append(or_(
            and_(literal(selected[rid]), member),
            User.id.in_(direct_ids),
            User.id.in_(select(groups.c.user_id).where(groups.c.assignment_id.in_(group_ids))),
        ))
    # Exclusion membership deliberately over-approximates user-key matching: a
    # multi-identity user can overlap a group without that group being a key.
    # Keep these candidates for the exact batched resolver below.
    candidate_filter = (or_(*base_bound) if binding_status == "bound" else or_(
        ~and_(*base_bound), User.id.in_(user_exclusions),
        User.id.in_(select(UserDepartment.user_id).where(UserDepartment.department_id.in_(department_exclusions))),
        *(User.id.in_(query) for query in exclusion_queries.values()),
    ))
    candidates = select(
        User.id, (func.row_number().over(order_by=(User.created_at.desc(), User.id)) - 1).label("position"),
    )
    availability = await user_option_availability(session, agent.id, resource_ids=resource_ids)
    if binding_status == "available":
        candidates = candidates.where(availability == "active")
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        candidates = candidates.where(or_(
            User.display_name.ilike(pattern), User.email.ilike(pattern), User.external_id.ilike(pattern),
        ))
    candidate_rows = candidates.cte("picker_candidates")
    query = select(User.id, User.display_name, candidate_rows.c.position, member.label("member"),
                   availability.label("availability")).join(
        candidate_rows, candidate_rows.c.id == User.id,
    ).where(candidate_filter).order_by(candidate_rows.c.position)
    items: list[dict[str, Any]] = []
    while True:
        rows = (await session.execute(query.where(candidate_rows.c.position >= offset).limit(100))).all()
        if not rows:
            return items, None
        ids = [row.id for row in rows]
        matching_groups: dict[str, set[str]] = {uid: set() for uid in ids}
        for uid, assignment_id in await session.execute(select(groups).where(groups.c.user_id.in_(ids))):
            matching_groups[uid].add(assignment_id)
        keys = await batch_user_knowledge_subjects(session, ids) if exclusion_keys else {}
        overlaps: dict[str, set[tuple[str, ...]]] = {uid: set() for uid in ids}
        complex_keys = list(exclusion_queries)
        # Direct user/department membership is already exact in keys. Only group
        # and source overlap needs SQL, bounded independently of exclusion count.
        for start in range(0, len(complex_keys), 25):
            overlap_query = union_all(*[
                select(exclusion_queries[key].subquery().c[0].label("user_id"), literal(index).label("key_index"))
                for index, key in enumerate(complex_keys[start:start + 25], start=start)
            ]).subquery()
            for uid, index in await session.execute(select(overlap_query).where(overlap_query.c.user_id.in_(ids))):
                overlaps[uid].add(complex_keys[index])
        for row in rows:
            inherited: dict[str, set[str]] = {}
            manual: set[str] = set()
            external: set[str] = set()
            matching_grants = list(user_grants.get(row.id, []))
            for group_id in matching_groups[row.id]:
                matching_grants.extend(group_grants.get(group_id, []))
            for grant, resources in matching_grants:
                if grant.user_id == row.id and grant.origin == AgentKnowledgeScopeOrigin.MANUAL:
                    manual.update(resources)
                if (grant.user_id == row.id and grant.origin != AgentKnowledgeScopeOrigin.MANUAL
                        or grant.group_assignment_id in matching_groups[row.id]):
                    for rid in resources:
                        inherited.setdefault(rid, set()).add(grant.source_name or grant.department_name or grant.name)
                    if grant.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC:
                        external.update(resources)
            count = 0
            partial = False
            sources: set[str] = set()
            for rid in resource_ids:
                excluded = bool(keys.get(row.id, set()) & exclusions[rid])
                default = selected[rid] and row.member
                partial |= bool(not excluded and default and overlaps[row.id] & exclusions[rid])
                count += rid in external or not excluded and (default or rid in manual or rid in inherited)
                if not excluded:
                    if default:
                        sources.add("SELECT_ALL" if rid in policies else "ALL_AGENT_KNOWLEDGE")
                    else:
                        sources.update(inherited.get(rid, set()))
            if binding_status == "bound" and not count:
                continue
            if binding_status == "available" and count == len(resource_ids) and not partial:
                continue
            if len(items) == limit:
                return items, row.position
            items.append({
                "subject": {"type": "USER", "user_id": row.id}, "label": row.display_name,
                "availability": row.availability,
                "status": "UNBOUND" if not count else "BOUND" if count == len(resource_ids) and not partial else "PARTIAL",
                "inheritance_sources": sorted(sources),
            })
        offset = rows[-1].position + 1
        if len(rows) < 100:
            return items, None
