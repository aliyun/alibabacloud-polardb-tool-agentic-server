from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.agent_access import list_agent_accessible_user_ids
from server.core.agent_knowledge_scope import resolve_agent_global_resource_scope
from server.enterprise_identity.service import identity_provider_key
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentKnowledgeScopeOrigin,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    ExternalKnowledgeScopeGrant,
    ExternalKnowledgeScopeSnapshot,
    KnowledgeResource,
    UserExternalIdentity,
)


SUPPORTED_SUBJECT_TYPES = frozenset({"user", "department", "chat", "group", "domain"})


class ExternalKnowledgeScopeSyncError(ValueError):
    pass


class ExternalKnowledgeScopeStaleSnapshotError(ExternalKnowledgeScopeSyncError):
    pass


@dataclass(frozen=True)
class ImportedGrant:
    subject_type: str
    subject_id: str


@dataclass(frozen=True)
class ImportedSnapshot:
    snapshot_id: str
    tenant_key: str
    source_scope_type: str
    source_scope_id: str
    source_scope_name: str
    space_id: str
    kb_id: str
    generation: str
    fingerprint: str
    status: str
    grants: tuple[ImportedGrant, ...]
    source_updated_at: datetime | None = None


def _encoded_resource(resource_id: str) -> str:
    return json.dumps([resource_id], separators=(",", ":"))


async def _resolve_import_target(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    snapshot: ImportedSnapshot,
) -> KnowledgeResource:
    resources = list(
        (
            await session.execute(
            select(KnowledgeResource)
            .join(
                EnterpriseIdentitySourceSpaceBinding,
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id
                == KnowledgeResource.knowledge_space_id,
            )
            .where(
                KnowledgeResource.space_id == snapshot.space_id,
                KnowledgeResource.kb_id == snapshot.kb_id,
                EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
            )
            .limit(2)
        )
        ).scalars()
    )
    if len(resources) > 1:
        raise ExternalKnowledgeScopeSyncError("knowledge resource target is ambiguous")
    if not resources:
        resource_exists = (
            await session.execute(
                select(KnowledgeResource.id)
                .where(
                    KnowledgeResource.space_id == snapshot.space_id,
                    KnowledgeResource.kb_id == snapshot.kb_id,
                )
                .limit(1)
            )
        ).first()
        if resource_exists is None:
            raise ExternalKnowledgeScopeSyncError("knowledge resource not found")
        raise ExternalKnowledgeScopeSyncError(
            "identity source is not bound to the target knowledge space"
        )
    return resources[0]


def _comparable_source_updated_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def lock_agent_knowledge_configuration(session: AsyncSession, agent_id: str) -> None:
    """Serialize mode changes and external projection without changing policy."""
    await session.execute(update(Agent).where(Agent.id == agent_id).values(
        knowledge_scope_mode=Agent.knowledge_scope_mode,
    ))


async def clear_agent_external_knowledge(session: AsyncSession, agent_id: str) -> int:
    """Discard derived scopes only; manual assignments and policies are retained."""
    ids = list(await session.scalars(select(AgentKnowledgeScope.id).where(
        AgentKnowledgeScope.agent_id == agent_id,
        AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
    )))
    if ids:
        await session.execute(delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id.in_(ids)))
        await session.execute(delete(AgentKnowledgeScope).where(AgentKnowledgeScope.id.in_(ids)))
    return len(ids)


async def _resolved_bindings(
    session: AsyncSession,
    *,
    agent: Agent,
    source: EnterpriseIdentitySource,
    grants: tuple[ImportedGrant, ...],
) -> list[AgentKnowledgeScopeBinding]:
    user_subjects = {
        grant.subject_id for grant in grants if grant.subject_type == "user"
    }
    user_ids: set[str] = set()
    if source.provider.value == "dingtalk":
        from server.models import EnterpriseDirectoryUser, EnterpriseDirectoryEntryStatus
        from server.enterprise_identity.service import identity_source_snapshot_is_usable
        if not identity_source_snapshot_is_usable(source):
            return []
        if any(g.subject_type == "domain" and g.subject_id != source.tenant_id for g in grants):
            raise ExternalKnowledgeScopeSyncError("domain does not match source tenant")
        # Resolve native organization predicates against trusted current directory facts.
        if any(g.subject_type in {"department", "domain"} for g in grants):
            from server.models import EnterpriseDirectoryMembership, EnterpriseDirectoryMembershipType
            department_ids = {g.subject_id for g in grants if g.subject_type == "department"}
            frontier = set(department_ids)
            visited = set()
            while frontier:
                rows = (await session.execute(select(EnterpriseDirectoryMembership).where(
                    EnterpriseDirectoryMembership.identity_source_id == source.id,
                    EnterpriseDirectoryMembership.external_group_id.in_(sorted(frontier)),
                ))).scalars().all()
                next_groups = set()
                for membership in rows:
                    if membership.member_type == EnterpriseDirectoryMembershipType.USER:
                        user_subjects.add(membership.external_member_id)
                    else:
                        next_groups.add(membership.external_member_id)
                visited.update(frontier)
                frontier = next_groups - visited
            if any(g.subject_type == "domain" for g in grants):
                user_subjects.update(await session.scalars(select(EnterpriseDirectoryUser.external_user_id).where(
                    EnterpriseDirectoryUser.identity_source_id == source.id,
                    EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE)))

    if source.provider.value == "dingtalk" and user_subjects:
        active = set(await session.scalars(select(EnterpriseDirectoryUser.external_user_id).where(
            EnterpriseDirectoryUser.identity_source_id == source.id,
            EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE)))
        user_subjects.intersection_update(active)
    # Bound IN parameters and directory resolution for large KB grants.
    ordered_users = sorted(user_subjects)
    for offset in range(0, len(ordered_users), 500):
        candidates = set(await session.scalars(select(UserExternalIdentity.user_id).where(
            UserExternalIdentity.identity_provider == identity_provider_key(source),
            UserExternalIdentity.external_subject.in_(ordered_users[offset:offset + 500]),
        )))
        user_ids.update(await list_agent_accessible_user_ids(session, agent, candidates))

    group_subjects = {
        grant.subject_id
        for grant in grants
        if grant.subject_type in {"department", "chat", "group"}
    }
    group_ids: set[str] = set()
    ordered_groups = sorted(group_subjects)
    for offset in range(0, len(ordered_groups), 500):
        group_ids.update(await session.scalars(select(AgentGroupAssignment.id).where(
            AgentGroupAssignment.agent_id == agent.id,
            AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE,
            AgentGroupAssignment.identity_source_id == source.id,
            AgentGroupAssignment.principal_id.in_(ordered_groups[offset:offset + 500]),
        )))

    return [
        *(AgentKnowledgeScopeBinding.for_user(scope_id="", user_id=value) for value in sorted(user_ids)),
        *(AgentKnowledgeScopeBinding.for_group(scope_id="", group_assignment_id=value) for value in sorted(group_ids)),
    ]


async def import_external_knowledge_scope_snapshot(
    session: AsyncSession,
    *,
    identity_source_id: str,
    snapshot: ImportedSnapshot,
    agent_ids: set[str] | None = None,
    reuse_unchanged_grants: bool = False,
) -> dict[str, int | str]:
    # Acquire Agent fences before snapshot locks, in a stable order. A mode
    # change must not be followed by a stale writer recreating derived scopes.
    candidates = select(Agent.id)
    if agent_ids is not None:
        candidates = candidates.where(Agent.id.in_(agent_ids))
    else:
        candidates = candidates.where(
            Agent.knowledge_scope_mode == AgentKnowledgeScopeMode.SCOPED,
            Agent.external_knowledge_auto_bind_enabled.is_(True),
        )
    for candidate_id in sorted(await session.scalars(candidates)):
        await lock_agent_knowledge_configuration(session, candidate_id)
    source = await session.get(EnterpriseIdentitySource, identity_source_id)
    if source is None:
        raise ExternalKnowledgeScopeSyncError("identity source not found")
    if source.provider.value not in {"feishu", "dingtalk"}:
        raise ExternalKnowledgeScopeSyncError("identity source provider is unsupported")
    if not source.tenant_id or source.tenant_id != snapshot.tenant_key:
        raise ExternalKnowledgeScopeSyncError("snapshot tenant does not match identity source")
    if not snapshot.snapshot_id or len(snapshot.snapshot_id) > 64:
        raise ExternalKnowledgeScopeSyncError("invalid snapshot id")
    for grant in snapshot.grants:
        if source.provider.value == "dingtalk" and grant.subject_type not in {"user", "department", "domain"}:
            raise ExternalKnowledgeScopeSyncError("unsupported DingTalk subject")
        if grant.subject_type == "domain" and (source.provider.value != "dingtalk" or grant.subject_id != source.tenant_id):
            raise ExternalKnowledgeScopeSyncError("domain does not match source tenant")
        if grant.subject_type not in SUPPORTED_SUBJECT_TYPES or not grant.subject_id:
            raise ExternalKnowledgeScopeSyncError("invalid grant subject")
    row = (
        await session.execute(
            select(ExternalKnowledgeScopeSnapshot)
            .where(ExternalKnowledgeScopeSnapshot.id == snapshot.snapshot_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    grants_unchanged = bool(reuse_unchanged_grants and row is not None
        and row.generation == snapshot.generation and row.fingerprint == snapshot.fingerprint)
    source_updated_at = snapshot.source_updated_at
    if row is not None:
        if row.identity_source_id != source.id:
            raise ExternalKnowledgeScopeSyncError("snapshot identity source mismatch")
        current_updated_at = _comparable_source_updated_at(row.source_updated_at)
        incoming_updated_at = _comparable_source_updated_at(
            snapshot.source_updated_at
        )
        if row.generation == snapshot.generation:
            if row.fingerprint != snapshot.fingerprint:
                raise ExternalKnowledgeScopeStaleSnapshotError(
                    "snapshot generation fingerprint conflict"
                )
            if current_updated_at is not None and (
                incoming_updated_at is None
                or incoming_updated_at < current_updated_at
            ):
                source_updated_at = row.source_updated_at
        elif (
            current_updated_at is None
            or incoming_updated_at is None
            or incoming_updated_at <= current_updated_at
        ):
            raise ExternalKnowledgeScopeStaleSnapshotError(
                "snapshot generation is stale or unordered"
            )
    resource = await _resolve_import_target(session, source, snapshot)
    if row is None:
        row = ExternalKnowledgeScopeSnapshot(
            id=snapshot.snapshot_id,
            identity_source_id=source.id,
            source_scope_type=snapshot.source_scope_type,
            source_scope_id=snapshot.source_scope_id,
            source_scope_name=snapshot.source_scope_name,
            knowledge_resource_id=resource.id,
            generation=snapshot.generation,
            fingerprint=snapshot.fingerprint,
            status=snapshot.status,
            source_updated_at=snapshot.source_updated_at,
        )
        session.add(row)
    else:
        row.source_scope_type = snapshot.source_scope_type
        row.source_scope_id = snapshot.source_scope_id
        row.source_scope_name = snapshot.source_scope_name
        row.knowledge_resource_id = resource.id
        row.generation = snapshot.generation
        row.fingerprint = snapshot.fingerprint
        row.status = snapshot.status
        row.source_updated_at = source_updated_at
    if not grants_unchanged:
        await session.execute(
            delete(ExternalKnowledgeScopeGrant).where(
                ExternalKnowledgeScopeGrant.snapshot_id == snapshot.snapshot_id
            )
        )
        for grant in sorted(set(snapshot.grants), key=lambda item: (item.subject_type, item.subject_id)):
            session.add(
                ExternalKnowledgeScopeGrant(
                    id=ExternalKnowledgeScopeGrant.deterministic_id(
                        snapshot.snapshot_id, grant.subject_type, grant.subject_id
                    ),
                    snapshot_id=snapshot.snapshot_id,
                    identity_source_id=source.id,
                    subject_type=grant.subject_type,
                    subject_id=grant.subject_id,
                )
            )
        await session.flush()

    agent_query = select(Agent).where(
        Agent.knowledge_scope_mode == AgentKnowledgeScopeMode.SCOPED,
        Agent.external_knowledge_auto_bind_enabled.is_(True),
    )
    if agent_ids is not None:
        agent_query = agent_query.where(Agent.id.in_(agent_ids))
    agents = list((await session.execute(agent_query.execution_options(populate_existing=True))).scalars())
    eligible: dict[str, Agent] = {}
    for agent in agents:
        base = await resolve_agent_global_resource_scope(session, agent.id)
        if base.allows(resource):
            eligible[agent.id] = agent

    existing_scope_query = select(AgentKnowledgeScope).where(
        AgentKnowledgeScope.identity_source_id == source.id,
        AgentKnowledgeScope.external_scope_id == snapshot.snapshot_id,
        AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
    )
    if agent_ids is not None:
        existing_scope_query = existing_scope_query.where(
            AgentKnowledgeScope.agent_id.in_(agent_ids)
        )
    existing_scopes = list((await session.execute(existing_scope_query)).scalars())
    existing_by_agent = {scope.agent_id: scope for scope in existing_scopes}
    projected = 0
    binding_count = 0
    active = snapshot.status == "ACTIVE"
    for agent_id, agent in eligible.items():
        scope = existing_by_agent.pop(agent_id, None)
        if scope is None:
            scope = AgentKnowledgeScope(
                id=AgentKnowledgeScope.external_id(agent.id, source.id, snapshot.snapshot_id),
                agent_id=agent.id,
                name=snapshot.source_scope_name or f"Feishu scope {snapshot.source_scope_id}",
                origin=AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
                identity_source_id=source.id,
                external_scope_id=snapshot.snapshot_id,
                knowledge_resource_ids_json=_encoded_resource(resource.id),
                created_by_user_id=None,
            )
            session.add(scope)
            await session.flush()
        else:
            scope.name = snapshot.source_scope_name or scope.name
            scope.knowledge_resource_ids_json = _encoded_resource(resource.id)
        await session.execute(
            delete(AgentKnowledgeScopeBinding).where(
                AgentKnowledgeScopeBinding.scope_id == scope.id
            )
        )
        if active:
            bindings = await _resolved_bindings(
                session, agent=agent, source=source, grants=snapshot.grants
            )
            for binding in bindings:
                binding.scope_id = scope.id
                # Constructors derive IDs from scope; rebuild after assigning it.
                if binding.user_id:
                    binding.id = AgentKnowledgeScopeBinding.for_user(
                        scope_id=scope.id, user_id=binding.user_id
                    ).id
                else:
                    binding.id = AgentKnowledgeScopeBinding.for_group(
                        scope_id=scope.id,
                        group_assignment_id=binding.group_assignment_id or "",
                    ).id
                session.add(binding)
            binding_count += len(bindings)
        projected += 1

    for scope in existing_by_agent.values():
        await session.execute(
            delete(AgentKnowledgeScopeBinding).where(
                AgentKnowledgeScopeBinding.scope_id == scope.id
            )
        )
        await session.delete(scope)
    await session.flush()
    return {
        "snapshot_id": snapshot.snapshot_id,
        "agents_projected": projected,
        "bindings_projected": binding_count,
    }


async def discard_external_knowledge_scope_snapshot(
    session: AsyncSession,
    *,
    identity_source_id: str,
    snapshot_id: str,
) -> None:
    """Remove a rejected snapshot and all of its derived authorization."""
    scope_ids = set(
        (
            await session.execute(
                select(AgentKnowledgeScope.id).where(
                    AgentKnowledgeScope.identity_source_id == identity_source_id,
                    AgentKnowledgeScope.external_scope_id == snapshot_id,
                    AgentKnowledgeScope.origin
                    == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
                )
            )
        ).scalars()
    )
    if scope_ids:
        await session.execute(
            delete(AgentKnowledgeScopeBinding).where(
                AgentKnowledgeScopeBinding.scope_id.in_(scope_ids)
            )
        )
        await session.execute(
            delete(AgentKnowledgeScope).where(AgentKnowledgeScope.id.in_(scope_ids))
        )
    await session.execute(
        delete(ExternalKnowledgeScopeGrant).where(
            ExternalKnowledgeScopeGrant.identity_source_id == identity_source_id,
            ExternalKnowledgeScopeGrant.snapshot_id == snapshot_id,
        )
    )
    await session.execute(
        delete(ExternalKnowledgeScopeSnapshot).where(
            ExternalKnowledgeScopeSnapshot.identity_source_id == identity_source_id,
            ExternalKnowledgeScopeSnapshot.id == snapshot_id,
        )
    )
    await session.flush()
