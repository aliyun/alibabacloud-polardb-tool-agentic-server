from __future__ import annotations

import json

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.agent_access import list_matching_agent_group_assignment_ids
from server.core.knowledge_subjects import SubjectKey, user_knowledge_subjects
from server.models import (
    Agent,
    AgentKnowledgePolicy,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentKnowledgeScopeOrigin,
    AgentPolarRAGInstanceBinding,
    KnowledgeResource,
)
from server.polarrag.access import KnowledgeResourceScope


def resource_ids_from_json(raw: str) -> set[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return set()
    return set(value)


def excluded_subjects(policy: AgentKnowledgePolicy) -> set[SubjectKey]:
    return {tuple(subject) for subject in json.loads(policy.excluded_subjects_json)}


async def knowledge_policies(session: AsyncSession, agent_id: str) -> dict[str, AgentKnowledgePolicy]:
    return {
        policy.knowledge_resource_id: policy
        for policy in await session.scalars(
            select(AgentKnowledgePolicy).where(AgentKnowledgePolicy.agent_id == agent_id)
        )
    }


async def resolve_agent_global_resource_scope(
    session: AsyncSession,
    agent_id: str,
) -> KnowledgeResourceScope:
    rows = (
        await session.execute(
            select(
                AgentPolarRAGInstanceBinding.polarrag_instance_id,
                AgentPolarRAGInstanceBinding.public_knowledge_resource_ids_json,
                AgentPolarRAGInstanceBinding.personal_knowledge_resource_ids_json,
                AgentPolarRAGInstanceBinding.public_excluded_resource_ids_json,
                AgentPolarRAGInstanceBinding.personal_excluded_resource_ids_json,
            ).where(AgentPolarRAGInstanceBinding.agent_id == agent_id)
        )
    ).all()
    bindings: dict[str, set[str] | None] = {}
    personal_bindings: dict[str, set[str] | None] = {}
    exclusion_filters = []
    for instance_id, raw, personal_raw, public_excluded, personal_excluded in rows:
        bindings[instance_id] = None if raw is None else resource_ids_from_json(raw)
        personal_bindings[instance_id] = None if personal_raw is None else resource_ids_from_json(personal_raw)
        for kb_type, excluded_raw in (("PUBLIC", public_excluded), ("PERSONAL", personal_excluded)):
            excluded = resource_ids_from_json(excluded_raw or "[]")
            if excluded:
                exclusion_filters.append(and_(
                    KnowledgeResource.polarrag_instance_id == instance_id,
                    KnowledgeResource.kb_type == kb_type,
                    KnowledgeResource.id.in_(excluded),
                ))
    exclusions = frozenset(await session.scalars(
        select(KnowledgeResource.id).where(or_(*exclusion_filters)),
    )) if exclusion_filters else frozenset()
    return KnowledgeResourceScope(
        bindings, personal_bindings=personal_bindings, excluded_resource_ids=exclusions,
    )


async def resolve_agent_knowledge_resource_scope(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
    *,
    manual_only: bool = False,
) -> KnowledgeResourceScope:
    base = await resolve_agent_global_resource_scope(session, agent_id)
    agent = await session.get(Agent, agent_id)
    if agent is None:
        return KnowledgeResourceScope({})
    instance_resource_ids = set(
        (
            await session.execute(
                select(KnowledgeResource.id)
                .where(KnowledgeResource.polarrag_instance_id.in_(sorted(base.bindings)))
            )
        ).scalars()
    )
    policies = await knowledge_policies(session, agent_id)
    allowed = instance_resource_ids - policies.keys() if agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL and not manual_only else set()
    subjects = await user_knowledge_subjects(session, user_id) if any(
        excluded_subjects(policy) for policy in policies.values()
    ) else {("USER", user_id)}
    denied: set[str] = set()
    for resource_id, policy in policies.items():
        if subjects & excluded_subjects(policy):
            denied.add(resource_id)
        elif policy.select_all:
            allowed.add(resource_id)

    group_ids = await list_matching_agent_group_assignment_ids(
        session,
        agent_id,
        user_id,
    )
    subject_filter = AgentKnowledgeScopeBinding.user_id == user_id
    if group_ids:
        subject_filter = or_(
            subject_filter,
            AgentKnowledgeScopeBinding.group_assignment_id.in_(group_ids),
        )
    rows = (
        await session.execute(
            select(AgentKnowledgeScope.knowledge_resource_ids_json, AgentKnowledgeScope.origin, AgentKnowledgeScope.identity_source_id)
            .join(
                AgentKnowledgeScopeBinding,
                AgentKnowledgeScopeBinding.scope_id == AgentKnowledgeScope.id,
            )
            .where(
                AgentKnowledgeScope.agent_id == agent_id,
                subject_filter,
                *([AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.MANUAL] if manual_only else []),
            )
            .distinct()
        )
    ).all()
    for raw, origin, source_id in rows:
        if origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC and source_id:
            from datetime import UTC, datetime, timedelta
            from server.models import EnterpriseIdentitySource
            from server.enterprise_identity.service import identity_source_snapshot_is_usable
            source = await session.get(EnterpriseIdentitySource, source_id)
            if source is not None and source.provider.value == "dingtalk":
                observed = source.permission_observed_at
                if observed is not None and observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                from server.models import EnterpriseDirectoryUser, EnterpriseDirectoryEntryStatus, UserExternalIdentity
                from server.enterprise_identity.service import identity_provider_key
                active_member = await session.scalar(select(EnterpriseDirectoryUser.id).join(
                    UserExternalIdentity,
                    UserExternalIdentity.external_subject == EnterpriseDirectoryUser.external_user_id,
                ).where(
                    EnterpriseDirectoryUser.identity_source_id == source.id,
                    EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                    UserExternalIdentity.user_id == user_id,
                    UserExternalIdentity.identity_provider == identity_provider_key(source),
                ).limit(1))
                if (not active_member or not identity_source_snapshot_is_usable(source) or observed is None
                        or observed < datetime.now(UTC) - timedelta(seconds=900)):
                    continue
        resources = resource_ids_from_json(raw)
        allowed.update(resources - denied if origin == AgentKnowledgeScopeOrigin.MANUAL else resources)
    # Exclusions constrain this KB's manual policy, not external-sync-owned grants.
    return KnowledgeResourceScope(
        base.bindings,
        allowed_resource_ids=allowed & instance_resource_ids,
        personal_bindings=base.personal_bindings,
        excluded_resource_ids=base.excluded_resource_ids,
    )
