from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Any, Iterable, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.enterprise_user_identity import enterprise_identities_by_user, resolve_enterprise_user_ids
from server.auth.dependencies import require_admin
from server.config import get_config
from server.core.agent_knowledge_scope import (
    excluded_subjects,
    knowledge_policies,
    resolve_agent_global_resource_scope,
    resource_ids_from_json,
)
from server.core.knowledge_subjects import (
    batch_group_knowledge_subjects,
    batch_user_knowledge_subjects,
    group_knowledge_subjects,
    knowledge_subject_members,
    user_knowledge_subjects,
)
from server.core.agent_access import has_agent_access, list_matching_agent_group_assignment_ids
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentKnowledgeScope,
    AgentKnowledgePolicy,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentKnowledgeScopeOrigin,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AuditStatus,
    Department,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    KnowledgeResource,
    KnowledgeResourceManagementMode,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
)
from server.polarrag.resource_selector import KnowledgePair, knowledge_resource_pairs


router = APIRouter(tags=["agent-knowledge-bindings"])

MAX_BATCH_OPERATIONS = 100
MAX_BATCH_SUBJECTS = 5_000
QUERY_CHUNK_SIZE = 500


class KnowledgeBindingOperationKind(str, enum.Enum):
    BIND = "BIND"
    UNBIND = "UNBIND"


class KnowledgeBindingSubjectType(str, enum.Enum):
    USER = "USER"
    DEPARTMENT = "DEPARTMENT"
    GROUP = "GROUP"
    IDENTITY_SOURCE = "IDENTITY_SOURCE"


class KnowledgeBindingSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: KnowledgeBindingSubjectType
    user_id: str | None = Field(default=None, min_length=1, max_length=36)
    department_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
    )
    identity_source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
    )
    external_user_id: str | None = Field(default=None, min_length=1, max_length=255)
    group_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_shape(self) -> KnowledgeBindingSubject:
        populated = {
            "user_id": self.user_id,
            "department_id": self.department_id,
            "identity_source_id": self.identity_source_id,
            "group_id": self.group_id,
            "external_user_id": self.external_user_id,
        }
        expected = {
            KnowledgeBindingSubjectType.USER: {"user_id"},
            KnowledgeBindingSubjectType.DEPARTMENT: {"department_id"},
            KnowledgeBindingSubjectType.GROUP: {
                "identity_source_id",
                "group_id",
            },
            KnowledgeBindingSubjectType.IDENTITY_SOURCE: {
                "identity_source_id",
            },
        }[self.type]
        if self.type == KnowledgeBindingSubjectType.USER and self.external_user_id is not None:
            expected = {"identity_source_id", "external_user_id"}
        if {key for key, value in populated.items() if value is not None} != expected:
            raise ValueError("subject fields do not match subject type")
        return self

    def key(self) -> tuple[str, ...]:
        if self.type == KnowledgeBindingSubjectType.USER:
            return (self.type.value, self.user_id or "")
        if self.type == KnowledgeBindingSubjectType.DEPARTMENT:
            return (self.type.value, self.department_id or "")
        if self.type == KnowledgeBindingSubjectType.IDENTITY_SOURCE:
            return (self.type.value, self.identity_source_id or "")
        return (
            self.type.value,
            self.identity_source_id or "",
            self.group_id or "",
        )


class KnowledgeBindingTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    knowledge_resource_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
    )
    space_id: str | None = Field(default=None, min_length=1, max_length=255)
    kb_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_shape(self) -> KnowledgeBindingTarget:
        has_resource = self.knowledge_resource_id is not None
        has_pair = self.space_id is not None and self.kb_id is not None
        if (self.space_id is None) != (self.kb_id is None):
            raise ValueError("space_id and kb_id must be provided together")
        if has_resource == has_pair:
            raise ValueError("provide exactly one knowledge_resource_id or space_id and kb_id")
        return self

    def key(self) -> tuple[str, ...]:
        if self.knowledge_resource_id is not None:
            return ("RESOURCE", self.knowledge_resource_id)
        return ("PAIR", self.space_id or "", self.kb_id or "")


class KnowledgeBindingOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: KnowledgeBindingOperationKind
    subjects: list[KnowledgeBindingSubject] = Field(
        min_length=1,
        max_length=MAX_BATCH_SUBJECTS,
    )
    targets: list[KnowledgeBindingTarget] = Field(min_length=1)


class KnowledgeBindingBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[KnowledgeBindingOperation] = Field(
        min_length=1,
        max_length=MAX_BATCH_OPERATIONS,
    )
    activate_scoped_mode: bool = False


class KnowledgeScopeModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: AgentKnowledgeScopeMode
    expected_mode: AgentKnowledgeScopeMode | None = None
    external_knowledge_auto_bind_enabled: bool | None = None
    reset_all_users_policies: bool = False


class ExternalKnowledgeBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: list[KnowledgeBindingSubject] = Field(
        min_length=1,
        max_length=MAX_BATCH_SUBJECTS,
    )
    targets: list[KnowledgeBindingTarget] = Field(min_length=1)
    activate_scoped_mode: bool = False


class KnowledgeScopeResponse(BaseModel):
    id: str
    agent_id: str
    name: str
    origin: AgentKnowledgeScopeOrigin
    identity_source_id: str | None
    external_scope_id: str | None
    knowledge_resource_ids: list[str]
    user_ids: list[str]
    enterprise_users: list[dict[str, Any]] = Field(default_factory=list)
    group_assignment_ids: list[str]
    created_at: datetime
    updated_at: datetime | None


class KnowledgeBindingSubjectResponse(BaseModel):
    enterprise_identities: list[dict[str, str]] = Field(default_factory=list)
    type: KnowledgeBindingSubjectType
    user_id: str | None = None
    department_id: str | None = None
    identity_source_id: str | None = None
    group_id: str | None = None
    display_name: str
    external_id: str | None = None


class KnowledgeResourceResponse(BaseModel):
    knowledge_resource_id: str
    space_id: str | None
    space_name: str | None
    kb_id: str | None
    name: str | None
    management_mode: KnowledgeResourceManagementMode | None
    identity_source_id: str | None
    identity_source_ids: list[str] = Field(default_factory=list)


class KnowledgeResourceOptionResponse(KnowledgeResourceResponse):
    kb_type: str = "PUBLIC"
    agent_bound: bool = True
    access_scope: Literal["ALL_USERS", "SELECTED", "UNCONFIGURED"] = "UNCONFIGURED"
    inheritance_sources: list[str] = Field(default_factory=list)


class KnowledgeBindingResponse(BaseModel):
    binding_id: str
    scope_id: str
    origin: AgentKnowledgeScopeOrigin
    identity_source_id: str | None
    external_scope_id: str | None
    subject: KnowledgeBindingSubjectResponse
    knowledge_resources: list[KnowledgeResourceResponse]
    created_at: datetime
    updated_at: datetime | None


class KnowledgeBindingListResponse(BaseModel):
    items: list[KnowledgeBindingResponse]
    total: int
    offset: int
    limit: int
    knowledge_scope_mode: AgentKnowledgeScopeMode


class KnowledgeResourceListResponse(BaseModel):
    items: list[KnowledgeResourceOptionResponse]
    total: int
    offset: int
    limit: int


class KnowledgeResourceSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_resource_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    targets: list[KnowledgePair] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_selector(self) -> KnowledgeResourceSelectionRequest:
        if len(self.model_fields_set & {"knowledge_resource_ids", "targets"}) != 1:
            raise ValueError("provide exactly one of knowledge_resource_ids or targets")
        if self.knowledge_resource_ids is None and self.targets is None:
            raise ValueError("knowledge selector cannot be null")
        return self

    def selection_targets(self) -> list[KnowledgeBindingTarget]:
        if self.targets is not None:
            return [KnowledgeBindingTarget(space_id=target.space_id, kb_id=target.kb_id) for target in self.targets]
        return [KnowledgeBindingTarget(knowledge_resource_id=value) for value in self.knowledge_resource_ids or []]


class KnowledgeBindingStatusRequest(KnowledgeResourceSelectionRequest):
    subjects: list[KnowledgeBindingSubject] = Field(
        min_length=1,
        max_length=MAX_BATCH_SUBJECTS,
    )


class KnowledgeBindingStatusItem(BaseModel):
    knowledge_resource_id: str
    space_id: str
    kb_id: str
    status: Literal["BOUND", "PARTIAL", "UNBOUND"]
    bound_subject_count: int
    subject_count: int
    bound_subject_indexes: list[int]
    direct_bound_subject_indexes: list[int] = Field(default_factory=list)
    partial_subject_indexes: list[int] = Field(default_factory=list)
    excluded_subject_indexes: list[int] = Field(default_factory=list)
    inherited_subject_sources: dict[int, list[str]] = Field(default_factory=dict)


class KnowledgeBindingStatusResponse(BaseModel):
    items: list[KnowledgeBindingStatusItem]


class KnowledgeSubjectOptionsRequest(KnowledgeResourceSelectionRequest):
    category: Literal["users", "groups", "sources"]
    binding_status: Literal["available", "bound"] = "available"
    search: str | None = Field(default=None, max_length=255)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class KnowledgeSubjectOption(BaseModel):
    enterprise_identities: list[dict[str, str]] = Field(default_factory=list)
    subject: KnowledgeBindingSubject
    label: str
    identity_source: str | None = None
    status: Literal["BOUND", "PARTIAL", "UNBOUND"]
    inheritance_sources: list[str]
    availability: Literal["active", "disabled", "unavailable"] = "active"


class KnowledgeSubjectOptionsResponse(BaseModel):
    items: list[KnowledgeSubjectOption]
    next_offset: int | None


class KnowledgeAllUsersRequest(KnowledgeResourceSelectionRequest):
    operation: KnowledgeBindingOperationKind


class KnowledgeAllUsersStatusRequest(KnowledgeResourceSelectionRequest):
    pass


class KnowledgeVisibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb_type: Literal["PUBLIC", "PERSONAL"]
    operation: KnowledgeBindingOperationKind


class KnowledgeVisibilityBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_resource_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    targets: list[KnowledgePair] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_selector(self) -> KnowledgeVisibilityBindRequest:
        if len(self.model_fields_set) != 1 or (self.knowledge_resource_ids is None and self.targets is None):
            raise ValueError("provide exactly one of knowledge_resource_ids or targets")
        return self


SubjectRef = tuple[str, str]


def _chunks(values: list[Any], size: int = QUERY_CHUNK_SIZE) -> Iterable[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _encoded_resource_ids(resource_ids: Iterable[str]) -> str:
    return json.dumps(sorted(set(resource_ids)), separators=(",", ":"))


async def _agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _resolve_targets(
    session: AsyncSession,
    agent: Agent,
    target_groups: list[list[KnowledgeBindingTarget]],
) -> dict[tuple[str, ...], str]:
    limit = get_config().polarrag_tool_limits.max_exhaustive_knowledge_resources
    for targets in target_groups:
        if len({target.key() for target in targets}) > limit:
            raise HTTPException(
                status_code=422,
                detail="Knowledge resource count exceeds configured limit",
            )

    target_keys = {target.key() for targets in target_groups for target in targets}
    resource_ids = sorted(key[1] for key in target_keys if key[0] == "RESOURCE")
    pairs = sorted((key[1], key[2]) for key in target_keys if key[0] == "PAIR")
    resources: dict[str, KnowledgeResource] = {}
    for chunk in _chunks(resource_ids):
        rows = (await session.execute(select(KnowledgeResource).where(KnowledgeResource.id.in_(chunk)))).scalars()
        resources.update((resource.id, resource) for resource in rows)
    pair_rows: dict[tuple[str, str], list[KnowledgeResource]] = {}
    for chunk in _chunks(pairs, 250):
        pair_resource_rows = (
            await session.execute(
                select(KnowledgeResource).where(tuple_(KnowledgeResource.space_id, KnowledgeResource.kb_id).in_(chunk))
            )
        ).scalars()
        for resource in pair_resource_rows:
            pair_rows.setdefault((resource.space_id, resource.kb_id), []).append(resource)

    candidate_space_ids = {resource.knowledge_space_id for resource in resources.values()}
    candidate_space_ids.update(
        resource.knowledge_space_id for pair_resources in pair_rows.values() for resource in pair_resources
    )
    bound_space_ids: set[str] = set()
    for chunk in _chunks(sorted(candidate_space_ids)):
        bound_space_ids.update(
            (
                await session.execute(
                    select(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id).where(
                        EnterpriseIdentitySourceSpaceBinding.knowledge_space_id.in_(chunk)
                    )
                )
            ).scalars()
        )

    global_scope = await resolve_agent_global_resource_scope(session, agent.id)
    resolved: dict[tuple[str, ...], str] = {}
    for key in target_keys:
        if key[0] == "RESOURCE":
            candidates = [resources[key[1]]] if key[1] in resources else []
        else:
            candidates = pair_rows.get((key[1], key[2]), [])
        candidates = [
            resource
            for resource in candidates
            if global_scope.allows(resource) and resource.knowledge_space_id in bound_space_ids
        ]
        if len(candidates) != 1:
            raise HTTPException(
                status_code=422,
                detail="Knowledge resource is missing, ambiguous, or outside the Agent scope",
            )
        resolved[key] = candidates[0].id
    return resolved


async def _existing_ids(
    session: AsyncSession,
    model: type[User] | type[Department],
    values: set[str],
) -> set[str]:
    existing: set[str] = set()
    for chunk in _chunks(sorted(values)):
        existing.update((await session.execute(select(model.id).where(model.id.in_(chunk)))).scalars())
    return existing


async def _validate_subject_space_bindings(
    session: AsyncSession,
    operations: list[KnowledgeBindingOperation],
    resolved_targets: dict[tuple[str, ...], str],
) -> None:
    resource_ids = sorted(set(resolved_targets.values()))
    resources: dict[str, str] = {}
    for chunk in _chunks(resource_ids):
        rows = (
            await session.execute(
                select(
                    KnowledgeResource.id,
                    KnowledgeResource.knowledge_space_id,
                ).where(KnowledgeResource.id.in_(chunk))
            )
        ).all()
        resources.update(rows)
    source_space_pairs: set[tuple[str, str]] = set()
    source_ids = sorted(
        {
            subject.identity_source_id
            for operation in operations
            for subject in operation.subjects
            if subject.type
            in (
                KnowledgeBindingSubjectType.GROUP,
                KnowledgeBindingSubjectType.IDENTITY_SOURCE,
            )
            and subject.identity_source_id
        }
    )
    if source_ids:
        source_space_pairs.update(
            (
                await session.execute(
                    select(
                        EnterpriseIdentitySourceSpaceBinding.identity_source_id,
                        EnterpriseIdentitySourceSpaceBinding.knowledge_space_id,
                    ).where(EnterpriseIdentitySourceSpaceBinding.identity_source_id.in_(source_ids))
                )
            ).all()
        )
    for operation in operations:
        target_space_ids = {resources[resolved_targets[target.key()]] for target in operation.targets}
        for subject in operation.subjects:
            if subject.type not in (
                KnowledgeBindingSubjectType.GROUP,
                KnowledgeBindingSubjectType.IDENTITY_SOURCE,
            ):
                continue
            source_id = subject.identity_source_id or ""
            if any(
                (source_id, knowledge_space_id) not in source_space_pairs for knowledge_space_id in target_space_ids
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Identity source must be bound to every target Space before configuring its users or groups"
                    ),
                )


async def _normalize_enterprise_subjects(
    session: AsyncSession, subjects: list[KnowledgeBindingSubject],
) -> None:
    by_source: dict[str, list[KnowledgeBindingSubject]] = {}
    for subject in subjects:
        if subject.type == KnowledgeBindingSubjectType.USER and subject.external_user_id is not None:
            by_source.setdefault(cast(str, subject.identity_source_id), []).append(subject)
    for source_id, group in by_source.items():
        ids = await resolve_enterprise_user_ids(session, source_id, [cast(str, item.external_user_id) for item in group])
        for subject, user_id in zip(group, ids):
            subject.user_id = user_id
            subject.identity_source_id = None
            subject.external_user_id = None


async def _resolve_subjects(
    session: AsyncSession,
    agent: Agent,
    subjects: list[KnowledgeBindingSubject],
    admin_id: str,
    *,
    create_assignment_keys: set[tuple[str, ...]] | None = None,
) -> dict[tuple[str, ...], SubjectRef]:
    by_key = {subject.key(): subject for subject in subjects}
    if len(by_key) > MAX_BATCH_SUBJECTS:
        raise HTTPException(status_code=422, detail="Subject count exceeds 5000")

    user_ids = {
        subject.user_id or "" for subject in by_key.values() if subject.type == KnowledgeBindingSubjectType.USER
    }
    department_ids = {
        subject.department_id or ""
        for subject in by_key.values()
        if subject.type == KnowledgeBindingSubjectType.DEPARTMENT
    }
    if await _existing_ids(session, User, user_ids) != user_ids:
        raise HTTPException(status_code=422, detail="User does not exist")
    if await _existing_ids(session, Department, department_ids) != department_ids:
        raise HTTPException(status_code=422, detail="Department does not exist")

    group_pairs = {
        (subject.identity_source_id or "", subject.group_id or "")
        for subject in by_key.values()
        if subject.type == KnowledgeBindingSubjectType.GROUP
    }
    existing_group_pairs: set[tuple[str, str]] = set()
    for chunk in _chunks(sorted(group_pairs), 250):
        group_rows = (
            await session.execute(
                select(
                    EnterpriseDirectoryGroup.identity_source_id,
                    EnterpriseDirectoryGroup.external_group_id,
                ).where(
                    tuple_(
                        EnterpriseDirectoryGroup.identity_source_id,
                        EnterpriseDirectoryGroup.external_group_id,
                    ).in_(chunk),
                    EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
        ).all()
        existing_group_pairs.update((source_id, group_id) for source_id, group_id in group_rows)
    if existing_group_pairs != group_pairs:
        raise HTTPException(
            status_code=422,
            detail="Identity source group does not exist",
        )

    identity_source_ids = {
        subject.identity_source_id or ""
        for subject in by_key.values()
        if subject.type == KnowledgeBindingSubjectType.IDENTITY_SOURCE
    }
    if await _existing_ids(session, EnterpriseIdentitySource, identity_source_ids) != identity_source_ids:
        raise HTTPException(status_code=422, detail="Identity source does not exist")

    candidates: dict[tuple[str, ...], AgentGroupAssignment] = {}
    for key, subject in by_key.items():
        if subject.type == KnowledgeBindingSubjectType.DEPARTMENT:
            candidates[key] = AgentGroupAssignment.for_department(
                agent_id=agent.id,
                department_id=subject.department_id or "",
                created_by_user_id=admin_id,
            )
        elif subject.type == KnowledgeBindingSubjectType.GROUP:
            candidates[key] = AgentGroupAssignment.for_identity_source_group(
                agent_id=agent.id,
                identity_source_id=subject.identity_source_id or "",
                external_group_id=subject.group_id or "",
                created_by_user_id=admin_id,
            )
        elif subject.type == KnowledgeBindingSubjectType.IDENTITY_SOURCE:
            candidates[key] = AgentGroupAssignment.for_identity_source_all_users(
                agent_id=agent.id,
                identity_source_id=subject.identity_source_id or "",
                created_by_user_id=admin_id,
            )

    existing_assignments: dict[str, AgentGroupAssignment] = {}
    assignment_keys = sorted({candidate.group_key for candidate in candidates.values()})
    for chunk in _chunks(assignment_keys):
        assignment_rows = (
            await session.execute(
                select(AgentGroupAssignment).where(
                    AgentGroupAssignment.agent_id == agent.id,
                    AgentGroupAssignment.group_key.in_(chunk),
                )
            )
        ).scalars()
        existing_assignments.update((row.group_key, row) for row in assignment_rows)
    for key, candidate in candidates.items():
        if candidate.group_key not in existing_assignments and (
            create_assignment_keys is None or key in create_assignment_keys
        ):
            session.add(candidate)
            existing_assignments[candidate.group_key] = candidate
    if candidates:
        await session.flush()

    existing_user_assignments: dict[str, AgentUserAssignment] = {}
    for chunk in _chunks(sorted(user_ids)):
        assignment_rows = (
            await session.execute(
                select(AgentUserAssignment).where(
                    AgentUserAssignment.agent_id == agent.id,
                    AgentUserAssignment.user_id.in_(chunk),
                )
            )
        ).scalars()
        existing_user_assignments.update((row.user_id, row) for row in assignment_rows)
    for key, subject in by_key.items():
        if (
            subject.type != KnowledgeBindingSubjectType.USER
            or create_assignment_keys is None
            or key not in create_assignment_keys
        ):
            continue
        user_id = subject.user_id or ""
        assignment = existing_user_assignments.get(user_id)
        if assignment is None:
            assignment = AgentUserAssignment(
                agent_id=agent.id,
                user_id=user_id,
                created_by_user_id=admin_id,
                is_direct=True,
            )
            session.add(assignment)
            existing_user_assignments[user_id] = assignment
        elif not assignment.is_direct:
            assignment.is_direct = True
            assignment.created_by_user_id = admin_id
    if create_assignment_keys is not None and user_ids:
        await session.flush()

    resolved: dict[tuple[str, ...], SubjectRef] = {}
    for key, subject in by_key.items():
        if subject.type == KnowledgeBindingSubjectType.USER:
            resolved[key] = ("user", subject.user_id or "")
        elif candidates[key].group_key in existing_assignments:
            resolved[key] = (
                "group",
                existing_assignments[candidates[key].group_key].id,
            )
    return resolved


async def _selected_manual_resources(
    session: AsyncSession,
    agent_id: str,
    subject_refs: set[SubjectRef],
) -> dict[SubjectRef, set[str]]:
    resources: dict[SubjectRef, set[str]] = {subject: set() for subject in subject_refs}
    user_ids = sorted(value for kind, value in subject_refs if kind == "user")
    group_ids = sorted(value for kind, value in subject_refs if kind == "group")
    for kind, values, column in (
        ("user", user_ids, AgentKnowledgeScopeBinding.user_id),
        ("group", group_ids, AgentKnowledgeScopeBinding.group_assignment_id),
    ):
        for chunk in _chunks(values):
            rows = (
                await session.execute(
                    select(column, AgentKnowledgeScope.knowledge_resource_ids_json)
                    .join(
                        AgentKnowledgeScope,
                        AgentKnowledgeScope.id == AgentKnowledgeScopeBinding.scope_id,
                    )
                    .where(
                        AgentKnowledgeScope.agent_id == agent_id,
                        AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.MANUAL,
                        column.in_(chunk),
                    )
                )
            ).all()
            for value, raw in rows:
                resources[(kind, value)].update(resource_ids_from_json(raw))
    return resources


async def _replace_selected_manual_bindings(
    session: AsyncSession,
    agent: Agent,
    desired: dict[SubjectRef, set[str]],
    admin_id: str,
) -> None:
    scopes = list(
        (
            await session.execute(
                select(AgentKnowledgeScope).where(
                    AgentKnowledgeScope.agent_id == agent.id,
                    AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.MANUAL,
                )
            )
        ).scalars()
    )
    reusable: dict[str, AgentKnowledgeScope] = {}
    for scope in scopes:
        reusable.setdefault(
            _encoded_resource_ids(resource_ids_from_json(scope.knowledge_resource_ids_json)),
            scope,
        )
    scope_ids = select(AgentKnowledgeScope.id).where(
        AgentKnowledgeScope.agent_id == agent.id,
        AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.MANUAL,
    )
    user_ids = sorted(value for kind, value in desired if kind == "user")
    group_ids = sorted(value for kind, value in desired if kind == "group")
    for values, column in (
        (user_ids, AgentKnowledgeScopeBinding.user_id),
        (group_ids, AgentKnowledgeScopeBinding.group_assignment_id),
    ):
        for chunk in _chunks(values):
            await session.execute(
                delete(AgentKnowledgeScopeBinding)
                .where(
                    AgentKnowledgeScopeBinding.scope_id.in_(scope_ids),
                    column.in_(chunk),
                )
                .execution_options(synchronize_session=False)
            )

    for subject, resource_ids in desired.items():
        if not resource_ids:
            continue
        encoded = _encoded_resource_ids(resource_ids)
        reusable_scope = reusable.get(encoded)
        if reusable_scope is None:
            reusable_scope = AgentKnowledgeScope(
                agent_id=agent.id,
                name="Manual knowledge bindings",
                origin=AgentKnowledgeScopeOrigin.MANUAL,
                knowledge_resource_ids_json=encoded,
                created_by_user_id=admin_id,
            )
            session.add(reusable_scope)
            await session.flush()
            reusable[encoded] = reusable_scope
        binding = (
            AgentKnowledgeScopeBinding.for_user(
                scope_id=reusable_scope.id,
                user_id=subject[1],
            )
            if subject[0] == "user"
            else AgentKnowledgeScopeBinding.for_group(
                scope_id=reusable_scope.id,
                group_assignment_id=subject[1],
            )
        )
        session.add(binding)
    await session.flush()

    bound_scope_ids: set[str] = set()
    for chunk in _chunks([scope.id for scope in scopes]):
        bound_scope_ids.update(
            (
                await session.execute(
                    select(AgentKnowledgeScopeBinding.scope_id).where(AgentKnowledgeScopeBinding.scope_id.in_(chunk))
                )
            ).scalars()
        )
    for scope in scopes:
        if scope.id not in bound_scope_ids:
            await session.delete(scope)


async def _replace_bindings(
    session: AsyncSession,
    scope: AgentKnowledgeScope,
    desired: set[SubjectRef],
) -> None:
    rows = list(
        (
            await session.execute(
                select(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id == scope.id)
            )
        ).scalars()
    )
    existing = {
        (
            "user" if row.user_id is not None else "group",
            row.user_id or row.group_assignment_id or "",
        ): row
        for row in rows
    }
    for subject, row in existing.items():
        if subject not in desired:
            await session.delete(row)
    for subject in desired - set(existing):
        binding = (
            AgentKnowledgeScopeBinding.for_user(
                scope_id=scope.id,
                user_id=subject[1],
            )
            if subject[0] == "user"
            else AgentKnowledgeScopeBinding.for_group(
                scope_id=scope.id,
                group_assignment_id=subject[1],
            )
        )
        session.add(binding)


async def _scope_response(
    session: AsyncSession,
    scope: AgentKnowledgeScope,
) -> KnowledgeScopeResponse:
    bindings = list(
        (
            await session.execute(
                select(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id == scope.id)
            )
        ).scalars()
    )
    user_ids = sorted(row.user_id for row in bindings if row.user_id)
    identities = await enterprise_identities_by_user(session, user_ids)
    return KnowledgeScopeResponse(
        id=scope.id,
        agent_id=scope.agent_id,
        name=scope.name,
        origin=scope.origin,
        identity_source_id=scope.identity_source_id,
        external_scope_id=scope.external_scope_id,
        knowledge_resource_ids=sorted(resource_ids_from_json(scope.knowledge_resource_ids_json)),
        user_ids=user_ids,
        enterprise_users=[{"user_id": user_id, "enterprise_identities": identities[user_id]} for user_id in user_ids],
        group_assignment_ids=sorted(row.group_assignment_id for row in bindings if row.group_assignment_id),
        created_at=scope.created_at,
        updated_at=scope.updated_at,
    )


async def _audit_agent(
    session: AsyncSession,
    admin: User,
    action: str,
    agent_id: str,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type="agent",
        target_id=agent_id,
        required=True,
        commit=False,
    )


def _resource_response(
    resource_id: str,
    resources: dict[str, KnowledgeResource],
    space_names: dict[str, str],
) -> KnowledgeResourceResponse:
    resource = resources.get(resource_id)
    if resource is None:
        return KnowledgeResourceResponse(
            knowledge_resource_id=resource_id,
            space_id=None,
            space_name=None,
            kb_id=None,
            name=None,
            management_mode=None,
            identity_source_id=None,
        )
    return KnowledgeResourceResponse(
        knowledge_resource_id=resource.id,
        space_id=resource.space_id,
        space_name=space_names.get(resource.knowledge_space_id),
        kb_id=resource.kb_id,
        name=resource.name,
        management_mode=resource.management_mode,
        identity_source_id=None,
    )


def _subject_response(row: Any, identities: dict[str, list[dict[str, str]]]) -> KnowledgeBindingSubjectResponse:
    if row.user_id is not None:
        return KnowledgeBindingSubjectResponse(
            type=KnowledgeBindingSubjectType.USER,
            user_id=row.user_id,
            enterprise_identities=identities.get(row.user_id, []),
            display_name=row.user_display_name or row.user_external_id or row.user_id,
            external_id=row.user_external_id,
        )
    if row.group_kind == AgentGroupKind.DEPARTMENT:
        return KnowledgeBindingSubjectResponse(
            type=KnowledgeBindingSubjectType.DEPARTMENT,
            department_id=row.department_id,
            display_name=row.department_name or row.department_id or row.group_assignment_id,
        )
    if row.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL:
        return KnowledgeBindingSubjectResponse(
            type=KnowledgeBindingSubjectType.IDENTITY_SOURCE,
            identity_source_id=row.group_identity_source_id,
            display_name=row.group_identity_source_name or row.group_identity_source_id or row.group_assignment_id,
        )
    return KnowledgeBindingSubjectResponse(
        type=KnowledgeBindingSubjectType.GROUP,
        identity_source_id=row.group_identity_source_id,
        group_id=row.group_principal_id,
        display_name=row.group_display_name or row.group_principal_id or row.group_assignment_id,
    )


@router.get(
    "/admin/agents/{agent_id}/knowledge-bindings",
    response_model=KnowledgeBindingListResponse,
)
async def list_knowledge_bindings(
    agent_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    origin: AgentKnowledgeScopeOrigin | None = None,
    subject_type: KnowledgeBindingSubjectType | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    group_join = AgentGroupAssignment.id == AgentKnowledgeScopeBinding.group_assignment_id
    directory_group_join = (EnterpriseDirectoryGroup.identity_source_id == AgentGroupAssignment.identity_source_id) & (
        EnterpriseDirectoryGroup.external_group_id == AgentGroupAssignment.principal_id
    )
    filters = [AgentKnowledgeScope.agent_id == agent_id]
    if origin is not None:
        filters.append(AgentKnowledgeScope.origin == origin)
    if subject_type == KnowledgeBindingSubjectType.USER:
        filters.append(AgentKnowledgeScopeBinding.user_id.is_not(None))
    elif subject_type == KnowledgeBindingSubjectType.DEPARTMENT:
        filters.append(AgentGroupAssignment.group_kind == AgentGroupKind.DEPARTMENT)
    elif subject_type == KnowledgeBindingSubjectType.GROUP:
        filters.append(AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE)
    elif subject_type == KnowledgeBindingSubjectType.IDENTITY_SOURCE:
        filters.append(AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL)
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                User.display_name.ilike(pattern),
                User.external_id.ilike(pattern),
                Department.name.ilike(pattern),
                EnterpriseDirectoryGroup.display_name.ilike(pattern),
                EnterpriseIdentitySource.name.ilike(pattern),
                AgentGroupAssignment.principal_id.ilike(pattern),
            )
        )
    query = (
        select(
            AgentKnowledgeScopeBinding.id.label("binding_id"),
            AgentKnowledgeScopeBinding.user_id,
            AgentKnowledgeScopeBinding.group_assignment_id,
            AgentKnowledgeScopeBinding.created_at,
            AgentKnowledgeScopeBinding.updated_at,
            AgentKnowledgeScope.id.label("scope_id"),
            AgentKnowledgeScope.origin,
            AgentKnowledgeScope.identity_source_id,
            AgentKnowledgeScope.external_scope_id,
            AgentKnowledgeScope.knowledge_resource_ids_json,
            User.display_name.label("user_display_name"),
            User.external_id.label("user_external_id"),
            AgentGroupAssignment.group_kind,
            AgentGroupAssignment.department_id,
            AgentGroupAssignment.identity_source_id.label("group_identity_source_id"),
            AgentGroupAssignment.principal_id.label("group_principal_id"),
            Department.name.label("department_name"),
            EnterpriseDirectoryGroup.display_name.label("group_display_name"),
            EnterpriseIdentitySource.name.label("group_identity_source_name"),
        )
        .join(
            AgentKnowledgeScope,
            AgentKnowledgeScope.id == AgentKnowledgeScopeBinding.scope_id,
        )
        .outerjoin(User, User.id == AgentKnowledgeScopeBinding.user_id)
        .outerjoin(AgentGroupAssignment, group_join)
        .outerjoin(Department, Department.id == AgentGroupAssignment.department_id)
        .outerjoin(EnterpriseDirectoryGroup, directory_group_join)
        .outerjoin(
            EnterpriseIdentitySource,
            EnterpriseIdentitySource.id == AgentGroupAssignment.identity_source_id,
        )
        .where(*filters)
    )
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await session.execute(
            query.order_by(
                AgentKnowledgeScopeBinding.created_at.desc(),
                AgentKnowledgeScopeBinding.id,
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    resource_ids = sorted(
        {resource_id for row in rows for resource_id in resource_ids_from_json(row.knowledge_resource_ids_json)}
    )
    resources: dict[str, KnowledgeResource] = {}
    for chunk in _chunks(resource_ids):
        resource_rows = (
            await session.execute(select(KnowledgeResource).where(KnowledgeResource.id.in_(chunk)))
        ).scalars()
        resources.update((resource.id, resource) for resource in resource_rows)
    space_names = {
        space.knowledge_space_id: space.name
        for space in (
            await session.execute(
                select(PolarRAGSpace).where(
                    PolarRAGSpace.knowledge_space_id.in_(
                        {resource.knowledge_space_id for resource in resources.values()}
                    )
                )
            )
        ).scalars()
    }
    identities = await enterprise_identities_by_user(session, [row.user_id for row in rows if row.user_id])
    return KnowledgeBindingListResponse(
        items=[
            KnowledgeBindingResponse(
                binding_id=row.binding_id,
                scope_id=row.scope_id,
                origin=row.origin,
                identity_source_id=row.identity_source_id,
                external_scope_id=row.external_scope_id,
                subject=_subject_response(row, identities),
                knowledge_resources=[
                    _resource_response(resource_id, resources, space_names)
                    for resource_id in sorted(resource_ids_from_json(row.knowledge_resource_ids_json))
                ],
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
        knowledge_scope_mode=agent.knowledge_scope_mode,
    )


async def _knowledge_grants(session: AsyncSession, agent_id: str) -> list[Any]:
    return list(
        (
            await session.execute(
                select(
                    AgentKnowledgeScope.knowledge_resource_ids_json,
                    AgentKnowledgeScope.origin,
                    AgentKnowledgeScope.name,
                    AgentKnowledgeScopeBinding.user_id,
                    AgentKnowledgeScopeBinding.group_assignment_id,
                    AgentGroupAssignment.group_kind,
                    AgentGroupAssignment.identity_source_id,
                    AgentGroupAssignment.principal_id,
                    EnterpriseIdentitySource.name.label("source_name"),
                    Department.name.label("department_name"),
                )
                .join(AgentKnowledgeScopeBinding, AgentKnowledgeScopeBinding.scope_id == AgentKnowledgeScope.id)
                .outerjoin(
                    AgentGroupAssignment, AgentGroupAssignment.id == AgentKnowledgeScopeBinding.group_assignment_id
                )
                .outerjoin(
                    EnterpriseIdentitySource, EnterpriseIdentitySource.id == AgentGroupAssignment.identity_source_id
                )
                .outerjoin(Department, Department.id == AgentGroupAssignment.department_id)
                .where(AgentKnowledgeScope.agent_id == agent_id)
            )
        ).all()
    )


async def _resource_access_summaries(session: AsyncSession, agent: Agent, resource_ids: set[str]) -> dict[str, dict]:
    policies = await knowledge_policies(session, agent.id)
    assigned = {
        ("user", user_id)
        for user_id in (
            await session.scalars(
                select(AgentUserAssignment.user_id).where(
                    AgentUserAssignment.agent_id == agent.id, AgentUserAssignment.is_direct.is_(True)
                )
            )
        ).all()
    }
    groups = list(
        (await session.scalars(select(AgentGroupAssignment).where(AgentGroupAssignment.agent_id == agent.id))).all()
    )
    assigned.update(("group", group.id) for group in groups)
    if agent.all_builtin_users:
        assigned.add(("builtin_all", agent.id))
    refs: dict[str, set[SubjectRef]] = {resource_id: set() for resource_id in resource_ids}
    sources: dict[str, set[str]] = {resource_id: set() for resource_id in resource_ids}
    for row in await _knowledge_grants(session, agent.id):
        for resource_id in resource_ids_from_json(row.knowledge_resource_ids_json) & resource_ids:
            refs[resource_id].add(("user", row.user_id) if row.user_id else ("group", row.group_assignment_id))
            if row.group_assignment_id or row.origin != AgentKnowledgeScopeOrigin.MANUAL:
                sources[resource_id].add(row.source_name or row.department_name or row.name)
    for resource_refs in refs.values():
        all_source_ids = {
            group.identity_source_id
            for group in groups
            if group.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL and ("group", group.id) in resource_refs
        }
        resource_refs.update(
            ("group", group.id)
            for group in groups
            if group.group_kind == AgentGroupKind.IDENTITY_SOURCE and group.identity_source_id in all_source_ids
        )
    missing_users = {
        user_id
        for resource_refs in refs.values()
        if any(kind == "group" for kind, _value in resource_refs)
        for kind, user_id in assigned - resource_refs
        if kind == "user"
    }
    for user_id in missing_users:
        matching = await list_matching_agent_group_assignment_ids(session, agent.id, user_id)
        for resource_refs in refs.values():
            if any(("group", group_id) in resource_refs for group_id in matching):
                resource_refs.add(("user", user_id))
    summaries = {
        resource_id: {
            "access_scope": "ALL_USERS"
            if assigned and assigned <= refs[resource_id]
            else "SELECTED"
            if refs[resource_id]
            else "UNCONFIGURED",
            "inheritance_sources": sorted(sources[resource_id]),
        }
        for resource_id in resource_ids
    }
    for resource_id in resource_ids:
        policy = policies.get(resource_id)
        if policy is not None and policy.select_all:
            summaries[resource_id] = {
                "access_scope": "SELECTED" if excluded_subjects(policy) else "ALL_USERS",
                "inheritance_sources": ["SELECT_ALL_EXCEPT" if excluded_subjects(policy) else "SELECT_ALL"],
            }
        elif policy is None and agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL:
            summaries[resource_id] = {"access_scope": "ALL_USERS", "inheritance_sources": ["ALL_AGENT_KNOWLEDGE"]}
    return summaries


@router.get(
    "/admin/agents/{agent_id}/knowledge-resource-options",
    response_model=KnowledgeResourceListResponse,
)
async def list_knowledge_resource_options(
    agent_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    management_mode: KnowledgeResourceManagementMode | None = None,
    binding_status: Literal["bound", "unbound"] | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    global_scope = await resolve_agent_global_resource_scope(session, agent_id)
    instance_ids = sorted(global_scope.bindings)
    if not instance_ids:
        return KnowledgeResourceListResponse(items=[], total=0, offset=offset, limit=limit)
    filters = [
        KnowledgeResource.polarrag_instance_id.in_(instance_ids),
        KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
        KnowledgeResource.enabled.is_(True),
        PolarRAGSpace.enabled.is_(True),
        select(EnterpriseIdentitySourceSpaceBinding.id)
        .where(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == KnowledgeResource.knowledge_space_id)
        .exists(),
    ]
    normalized_search = (search or "").strip()
    # Deprecated management_mode is accepted for older clients but no longer filters binding options.
    if binding_status is not None:
        bound_conditions = [KnowledgeResource.kb_type.not_in(("PUBLIC", "PERSONAL"))]
        for kb_type, bindings in (
            ("PUBLIC", global_scope.bindings),
            ("PERSONAL", global_scope.personal_bindings or {}),
        ):
            for instance_id, selected in bindings.items():
                condition = (KnowledgeResource.polarrag_instance_id == instance_id) & (KnowledgeResource.kb_type == kb_type)
                if selected is not None:
                    condition &= KnowledgeResource.id.in_(sorted(selected))
                bound_conditions.append(condition)
        bound = or_(*bound_conditions)
        if global_scope.excluded_resource_ids:
            bound &= KnowledgeResource.id.not_in(sorted(global_scope.excluded_resource_ids))
        filters.append(bound if binding_status == "bound" else ~bound)
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                KnowledgeResource.name.ilike(pattern),
                KnowledgeResource.kb_id.ilike(pattern),
                PolarRAGSpace.name.ilike(pattern),
            )
        )
    query = (
        select(KnowledgeResource)
        .join(
            PolarRAGSpace,
            PolarRAGSpace.knowledge_space_id == KnowledgeResource.knowledge_space_id,
        )
        .where(*filters)
    )
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    resources = list(
        (
            await session.execute(
                query.order_by(
                    PolarRAGSpace.name,
                    KnowledgeResource.name,
                    KnowledgeResource.id,
                )
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    spaces = {
        space.knowledge_space_id: space
        for space in (
            await session.execute(
                select(PolarRAGSpace).where(
                    PolarRAGSpace.knowledge_space_id.in_({resource.knowledge_space_id for resource in resources})
                )
            )
        ).scalars()
    }
    source_ids_by_space: dict[str, list[str]] = {}
    if spaces:
        source_rows = (
            await session.execute(
                select(
                    EnterpriseIdentitySourceSpaceBinding.knowledge_space_id,
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id,
                ).where(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id.in_(spaces))
            )
        ).all()
        for knowledge_space_id, identity_source_id in source_rows:
            source_ids_by_space.setdefault(knowledge_space_id, []).append(identity_source_id)
    summaries = await _resource_access_summaries(session, agent, {resource.id for resource in resources})
    for resource in resources:
        if not global_scope.allows(resource):
            summaries[resource.id] = {"access_scope": "UNCONFIGURED", "inheritance_sources": []}
    return KnowledgeResourceListResponse(
        items=[
            KnowledgeResourceOptionResponse(
                kb_type=resource.kb_type,
                agent_bound=global_scope.allows(resource),
                knowledge_resource_id=resource.id,
                space_id=resource.space_id,
                space_name=spaces[resource.knowledge_space_id].name,
                kb_id=resource.kb_id,
                name=resource.name,
                management_mode=resource.management_mode,
                identity_source_id=(
                    source_ids_by_space[resource.knowledge_space_id][0]
                    if len(source_ids_by_space.get(resource.knowledge_space_id, [])) == 1
                    else None
                ),
                identity_source_ids=sorted(source_ids_by_space.get(resource.knowledge_space_id, [])),
                **summaries[resource.id],
            )
            for resource in resources
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/admin/agents/{agent_id}/knowledge-binding-status",
    response_model=KnowledgeBindingStatusResponse,
)
async def get_knowledge_binding_status(
    agent_id: str,
    body: KnowledgeBindingStatusRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    await _normalize_enterprise_subjects(session, body.subjects)
    resolved_subjects = await _resolve_subjects(
        session,
        agent,
        body.subjects,
        admin.id,
        create_assignment_keys=set(),
    )
    targets = body.selection_targets()
    resolved_targets = await _resolve_targets(session, agent, [targets])
    subject_refs = [resolved_subjects.get(subject.key()) for subject in body.subjects]
    current = await _selected_manual_resources(session, agent.id, {ref for ref in subject_refs if ref is not None})
    resource_ids = set(resolved_targets.values())
    policies = {resource_id: policy for resource_id, policy in (await knowledge_policies(session, agent.id)).items()
                if resource_id in resource_ids}
    grants = [row for row in await _knowledge_grants(session, agent.id)
              if resource_ids & resource_ids_from_json(row.knowledge_resource_ids_json)]
    default_all = agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL
    default_indexes: set[int] = set()
    key_cache: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    exclusions = set().union(*(excluded_subjects(policy) for policy in policies.values()))
    if exclusions:
        user_ids = {subject.user_id or "" for subject in body.subjects
                    if subject.type == KnowledgeBindingSubjectType.USER}
        user_ids.update(key[1] for key in exclusions if key[0] == "USER")
        for chunk in _chunks(sorted(user_ids)):
            key_cache.update((("USER", uid), keys)
                             for uid, keys in (await batch_user_knowledge_subjects(session, chunk)).items())
    if default_all or any(policy.select_all for policy in policies.values()) or exclusions or any(
        row.group_kind == AgentGroupKind.IDENTITY_SOURCE for row in grants
    ):
        groups = {(subject.identity_source_id or "", subject.group_id or "") for subject in body.subjects
                  if subject.type == KnowledgeBindingSubjectType.GROUP}
        groups.update((key[1], key[2]) for key in exclusions if key[0] == "GROUP")
        key_cache.update(await batch_group_knowledge_subjects(session, groups))
    if default_all or any(policy.select_all for policy in policies.values()):
        assigned_sources = set((await session.execute(
            select(AgentGroupAssignment.identity_source_id).where(
                AgentGroupAssignment.agent_id == agent.id,
                AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL,
            )
        )).scalars())
        assigned_groups = {
            ("GROUP", source_id, group_id)
            for source_id, group_id in (await session.execute(
                select(AgentGroupAssignment.identity_source_id, AgentGroupAssignment.principal_id).where(
                    AgentGroupAssignment.agent_id == agent.id,
                    AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE,
                )
            )).all()
        }
        for index, (subject, ref) in enumerate(zip(body.subjects, subject_refs)):
            if subject.type == KnowledgeBindingSubjectType.USER:
                member = await has_agent_access(session, agent.id, subject.user_id or "")
            else:
                member = ref is not None or (
                    subject.type == KnowledgeBindingSubjectType.GROUP
                    and subject.identity_source_id in assigned_sources
                )
                if not member and subject.type == KnowledgeBindingSubjectType.GROUP and assigned_groups:
                    key = subject.key()
                    if key not in key_cache:
                        key_cache[key] = await group_knowledge_subjects(session, key[1], key[2])
                    member = bool(key_cache[key] & assigned_groups)
            if member:
                default_indexes.add(index)
    inherited: list[dict[str, set[str]]] = []
    external: list[set[str]] = []
    for subject, ref in zip(body.subjects, subject_refs):
        group_keys: set[tuple[str, ...]] = set()
        if subject.type == KnowledgeBindingSubjectType.GROUP and any(
            row.group_kind == AgentGroupKind.IDENTITY_SOURCE for row in grants
        ):
            key = subject.key()
            if key not in key_cache:
                key_cache[key] = await group_knowledge_subjects(session, key[1], key[2])
            group_keys = key_cache[key]
        matching_groups = (
            await list_matching_agent_group_assignment_ids(session, agent.id, subject.user_id)
            if subject.type == KnowledgeBindingSubjectType.USER
            and any(row.group_assignment_id for row in grants)
            else set()
        )
        inherited_resources: dict[str, set[str]] = {}
        external_resources: set[str] = set()
        for row in grants:
            row_ref = ("user", row.user_id) if row.user_id else ("group", row.group_assignment_id)
            covered = (
                row_ref == ref
                and row.origin != AgentKnowledgeScopeOrigin.MANUAL
                or row.group_assignment_id in matching_groups
                or subject.type == KnowledgeBindingSubjectType.GROUP
                and row.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL
                and row.identity_source_id == subject.identity_source_id
                or subject.type == KnowledgeBindingSubjectType.GROUP
                and row.group_kind == AgentGroupKind.IDENTITY_SOURCE
                and row_ref != ref
                and ("GROUP", row.identity_source_id, row.principal_id) in group_keys
            )
            if covered:
                for resource_id in resource_ids_from_json(row.knowledge_resource_ids_json):
                    inherited_resources.setdefault(resource_id, set()).add(
                        row.source_name or row.department_name or row.name
                    )
                    if row.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC:
                        external_resources.add(resource_id)
        inherited.append(inherited_resources)
        external.append(external_resources)
    member_queries: dict[tuple[str, ...], Any] = {}
    overlap_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], bool] = {}

    async def overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
        pair = tuple(sorted((left, right)))
        if pair not in overlap_cache:
            for key in pair:
                if key not in member_queries:
                    member_queries[key] = await knowledge_subject_members(session, key)
            overlap_cache[pair] = await session.scalar(
                select(User.id).where(
                    User.id.in_(member_queries[left]), User.id.in_(member_queries[right]),
                ).limit(1)
            ) is not None
        return overlap_cache[pair]

    async def matching_keys(key: tuple[str, ...]) -> set[tuple[str, ...]]:
        if key not in key_cache:
            if key[0] == "USER":
                key_cache[key] = await user_knowledge_subjects(session, key[1])
            elif key[0] == "GROUP":
                key_cache[key] = await group_knowledge_subjects(session, key[1], key[2])
            else:
                key_cache[key] = {key}
        return key_cache[key]
    items: list[KnowledgeBindingStatusItem] = []
    resource_pairs = await knowledge_resource_pairs(session, resolved_targets.values())
    for resolved_resource_id in dict.fromkeys(resolved_targets[target.key()] for target in targets):
        policy = policies.get(resolved_resource_id)
        all_selected = policy.select_all if policy is not None else default_all
        excluded = excluded_subjects(policy) if policy is not None else set()
        excluded_indexes: set[int] = set()
        partial_indexes: set[int] = set()
        for index, subject in enumerate(body.subjects):
            key = subject.key()
            keys = await matching_keys(key) if excluded else {key}
            if keys & excluded:
                excluded_indexes.add(index)
            elif all_selected and index in default_indexes and excluded:
                for exclusion in excluded:
                    if key in await matching_keys(exclusion) or await overlaps(key, exclusion):
                        partial_indexes.add(index)
                        break
        bound_indexes = [
            index
            for index, subject_ref in enumerate(subject_refs)
            if resolved_resource_id in external[index] or index not in excluded_indexes and (
                all_selected and index in default_indexes
                or resolved_resource_id in current.get(subject_ref, set())
                or resolved_resource_id in inherited[index]
            )
        ]
        if not bound_indexes:
            status: Literal["BOUND", "PARTIAL", "UNBOUND"] = "UNBOUND"
        elif len(bound_indexes) == len(subject_refs) and not partial_indexes:
            status = "BOUND"
        else:
            status = "PARTIAL"
        items.append(
            KnowledgeBindingStatusItem(
                knowledge_resource_id=resolved_resource_id,
                **resource_pairs[resolved_resource_id],
                status=status,
                bound_subject_count=len(bound_indexes),
                subject_count=len(subject_refs),
                bound_subject_indexes=bound_indexes,
                partial_subject_indexes=sorted(partial_indexes),
                excluded_subject_indexes=sorted(excluded_indexes),
                direct_bound_subject_indexes=[
                    index for index, ref in enumerate(subject_refs)
                    if resolved_resource_id in current.get(ref, set()) or all_selected and index in default_indexes
                ],
                inherited_subject_sources={
                    index: (
                        ["SELECT_ALL" if policy is not None else "ALL_AGENT_KNOWLEDGE"]
                        if all_selected and index in default_indexes
                        else sorted(inherited[index][resolved_resource_id])
                    )
                    for index in range(len(subject_refs))
                    if index not in excluded_indexes
                    and (all_selected and index in default_indexes or resolved_resource_id in inherited[index])
                },
            )
        )
    return KnowledgeBindingStatusResponse(items=items)


@router.post(
    "/admin/agents/{agent_id}/knowledge-subject-options",
    response_model=KnowledgeSubjectOptionsResponse,
)
async def list_knowledge_subject_options(
    agent_id: str,
    body: KnowledgeSubjectOptionsRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.api.agent_polarrag_access import list_group_options
    from server.enterprise_identity.service import identity_source_snapshot_is_usable

    agent = await _agent(session, agent_id)
    targets = body.selection_targets()
    resolved = await _resolve_targets(session, agent, [targets])
    resource_ids = list(dict.fromkeys(resolved[target.key()] for target in targets))
    if body.category == "users":
        from server.api.agent_polarrag_access import _user_identity_source_names
        from server.core.knowledge_user_options import user_options

        user_items, next_offset = await user_options(
            session, agent, resource_ids, await _knowledge_grants(session, agent.id),
            search=body.search, binding_status=body.binding_status, offset=body.offset, limit=body.limit,
        )
        names = await _user_identity_source_names(session, [item["subject"]["user_id"] for item in user_items])
        identities = await enterprise_identities_by_user(session, [item["subject"]["user_id"] for item in user_items])
        return KnowledgeSubjectOptionsResponse(items=[KnowledgeSubjectOption(
            **item, enterprise_identities=identities[item["subject"]["user_id"]], identity_source=" / ".join(names.get(item["subject"]["user_id"], [])) or "PAS",
        ) for item in user_items], next_offset=next_offset)
    if body.binding_status == "bound":
        policies = await knowledge_policies(session, agent.id)
        selected_all = any(
            policies[resource_id].select_all if resource_id in policies
            else agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL
            for resource_id in resource_ids
        )
        if not selected_all and not any(
            row.group_assignment_id and set(resource_ids) & resource_ids_from_json(row.knowledge_resource_ids_json)
            for row in await _knowledge_grants(session, agent.id)
        ):
            # Without select-all, bound group/source subjects require a target-KB group-assignment grant.
            return KnowledgeSubjectOptionsResponse(items=[], next_offset=None)
    space_ids = set((await session.execute(
        select(KnowledgeResource.knowledge_space_id).where(KnowledgeResource.id.in_(resource_ids))
    )).scalars())
    sources = (await session.execute(
        select(EnterpriseIdentitySource)
        .join(EnterpriseIdentitySourceSpaceBinding)
        .where(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id.in_(space_ids))
        .group_by(EnterpriseIdentitySource.id)
        .having(func.count(func.distinct(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id)) == len(space_ids))
    )).scalars()
    source_ids = {source.id for source in sources if identity_source_snapshot_is_usable(source)}
    if body.category == "sources" and not source_ids:
        return KnowledgeSubjectOptionsResponse(items=[], next_offset=None)

    items: list[KnowledgeSubjectOption] = []
    offset = body.offset
    # Scan bounded candidate pages, reusing the effective-grant resolver. The cursor
    # advances over candidates, not filtered rows, so inherited/partial grants do
    # not create short pages or require loading the whole directory into the UI.
    while True:
        params = dict(agent_id=agent_id, search=body.search, binding_status="all",
                      offset=offset, limit=100, _admin=admin, session=session)
        candidates: list[tuple[int, KnowledgeBindingSubject, str, str | None]] = []
        page = await list_group_options(
            **params, category="identity_source" if body.category == "sources" else "group",
            identity_source_ids=",".join(sorted(source_ids)) or None,
        )
        group_pairs = {
            (row.identity_source_id, row.principal_id)
            for row in page.items if row.group_kind == AgentGroupKind.IDENTITY_SOURCE
        }
        active_groups = set((await session.execute(
            select(EnterpriseDirectoryGroup.identity_source_id, EnterpriseDirectoryGroup.external_group_id)
            .where(
                tuple_(EnterpriseDirectoryGroup.identity_source_id, EnterpriseDirectoryGroup.external_group_id)
                .in_(group_pairs),
                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            )
        )).tuples()) if group_pairs else set()
        for index, row in enumerate(page.items):
            if row.identity_source_id and row.identity_source_id not in source_ids:
                continue
            if row.group_kind == AgentGroupKind.DEPARTMENT:
                subject = KnowledgeBindingSubject(type="DEPARTMENT", department_id=row.department_id)
                label = row.department_name or row.department_id
            elif row.group_kind == AgentGroupKind.IDENTITY_SOURCE:
                if (row.identity_source_id, row.principal_id) not in active_groups:
                    continue
                subject = KnowledgeBindingSubject(type="GROUP", identity_source_id=row.identity_source_id,
                                                  group_id=row.principal_id)
                label = row.external_group_name or row.principal_id
            elif row.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL:
                subject = KnowledgeBindingSubject(type="IDENTITY_SOURCE", identity_source_id=row.identity_source_id)
                label = row.identity_source_name or row.identity_source_id
            else:
                continue
            candidates.append((index, subject, label, row.identity_source_name))
        if candidates:
            status = await get_knowledge_binding_status(
                agent_id, KnowledgeBindingStatusRequest(
                    subjects=[row[1] for row in candidates], knowledge_resource_ids=resource_ids,
                ), admin, session,
            )
            for subject_index, (candidate_index, subject, label, source_name) in enumerate(candidates):
                count = sum(subject_index in row.bound_subject_indexes for row in status.items)
                partial = any(subject_index in row.partial_subject_indexes for row in status.items)
                if body.binding_status == "available" and count == len(resource_ids) and not partial:
                    continue
                if body.binding_status == "bound" and count == 0:
                    continue
                if len(items) == body.limit:
                    return KnowledgeSubjectOptionsResponse(items=items, next_offset=offset + candidate_index)
                items.append(KnowledgeSubjectOption(
                    subject=subject, label=label, identity_source=source_name,
                    status="UNBOUND" if count == 0 else "BOUND" if count == len(resource_ids) and not partial else "PARTIAL",
                    inheritance_sources=sorted({
                        source for row in status.items
                        for source in row.inherited_subject_sources.get(subject_index, [])
                    }),
                ))
        offset += len(page.items)
        if not page.items or offset >= page.total:
            return KnowledgeSubjectOptionsResponse(items=items, next_offset=None)


@router.post("/admin/agents/{agent_id}/knowledge-bindings:batch")
async def update_knowledge_bindings_batch(
    agent_id: str,
    body: KnowledgeBindingBatchRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await session.execute(update(Agent).where(Agent.id == agent_id).values(knowledge_scope_mode=Agent.knowledge_scope_mode))
    agent = await _agent(session, agent_id)
    policies = await knowledge_policies(session, agent_id)
    all_subjects = [subject for operation in body.operations for subject in operation.subjects]
    await _normalize_enterprise_subjects(session, all_subjects)
    bind_subject_keys = {
        subject.key()
        for operation in body.operations
        if operation.operation == KnowledgeBindingOperationKind.BIND
        for subject in operation.subjects
    }
    resolved_subjects = await _resolve_subjects(
        session,
        agent,
        all_subjects,
        admin.id,
        create_assignment_keys=bind_subject_keys,
    )
    resolved_targets = await _resolve_targets(
        session,
        agent,
        [operation.targets for operation in body.operations],
    )
    await _validate_subject_space_bindings(
        session,
        body.operations,
        resolved_targets,
    )
    subject_refs = set(resolved_subjects.values())
    desired = await _selected_manual_resources(session, agent.id, subject_refs)
    for operation in body.operations:
        operation_subjects = {
            resolved_subjects[subject.key()] for subject in operation.subjects if subject.key() in resolved_subjects
        }
        operation_targets = {resolved_targets[target.key()] for target in operation.targets}
        for resource_id in operation_targets:
            policy = policies.get(resource_id)
            if policy is None and agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL and not body.activate_scoped_mode:
                policy = AgentKnowledgePolicy(
                    agent_id=agent.id, knowledge_resource_id=resource_id, select_all=True,
                    excluded_subjects_json="[]",
                )
                session.add(policy)
                policies[resource_id] = policy
            if policy is not None:
                excluded = excluded_subjects(policy)
                keys = {subject.key() for subject in operation.subjects}
                if operation.operation == KnowledgeBindingOperationKind.BIND:
                    excluded.difference_update(keys)
                elif policy.select_all:
                    excluded.update(keys)
                policy.excluded_subjects_json = json.dumps(sorted(excluded), separators=(",", ":"))
        for subject in operation_subjects:
            if operation.operation == KnowledgeBindingOperationKind.BIND:
                desired[subject].update(operation_targets)
            else:
                desired[subject].difference_update(operation_targets)
    await _replace_selected_manual_bindings(session, agent, desired, admin.id)
    if body.activate_scoped_mode:
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
    await _audit_agent(
        session,
        admin,
        "agent_knowledge_binding.batch_update",
        agent.id,
    )
    await session.commit()
    return {
        "agent_id": agent.id,
        "operations_processed": len(body.operations),
        "knowledge_scope_mode": agent.knowledge_scope_mode.value,
    }


@router.post("/admin/agents/{agent_id}/knowledge-all-users-status")
async def get_knowledge_all_users_status(
    agent_id: str,
    body: KnowledgeAllUsersStatusRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    resolved = await _resolve_targets(session, agent, [body.selection_targets()])
    policies = await knowledge_policies(session, agent_id)
    grants = await _knowledge_grants(session, agent_id)
    granted = set().union(*(resource_ids_from_json(row.knowledge_resource_ids_json) for row in grants))
    resource_pairs = await knowledge_resource_pairs(session, resolved.values())
    items = []
    for resource_id in dict.fromkeys(resolved.values()):
        policy = policies.get(resource_id)
        select_all = policy.select_all if policy is not None else agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL
        exclusions = excluded_subjects(policy) if policy is not None else set()
        items.append({
            "knowledge_resource_id": resource_id,
            **resource_pairs[resource_id],
            "select_all": select_all,
            "excluded_subject_count": len(exclusions),
            "status": "BOUND" if select_all and not exclusions else "PARTIAL"
            if select_all or resource_id in granted else "UNBOUND",
        })
    return {"items": items}


@router.get("/admin/agents/{agent_id}/knowledge-visibility")
async def get_knowledge_visibility(
    agent_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _agent(session, agent_id)
    bindings = list(await session.scalars(select(AgentPolarRAGInstanceBinding).where(
        AgentPolarRAGInstanceBinding.agent_id == agent_id,
    )))
    result = {}
    for kb_type, field in (
        ("PUBLIC", "public_knowledge_resource_ids_json"),
        ("PERSONAL", "personal_knowledge_resource_ids_json"),
    ):
        values = [getattr(binding, field) for binding in bindings]
        exclusions = [resource_ids_from_json(
            getattr(binding, f"{kb_type.lower()}_excluded_resource_ids_json") or "[]",
        ) for binding in bindings]
        result[kb_type] = (
            "BOUND" if values and all(value is None for value in values) and not any(exclusions)
            else "UNBOUND" if all(value is not None and not resource_ids_from_json(value) for value in values)
            else "PARTIAL"
        )
    return {"instance_count": len(bindings), "statuses": result}


@router.post("/admin/agents/{agent_id}/knowledge-visibility")
async def update_knowledge_visibility(
    agent_id: str,
    body: KnowledgeVisibilityRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await session.execute(update(Agent).where(Agent.id == agent_id).values(knowledge_scope_mode=Agent.knowledge_scope_mode))
    await _agent(session, agent_id)
    field = (
        "public_knowledge_resource_ids_json" if body.kb_type == "PUBLIC"
        else "personal_knowledge_resource_ids_json"
    )
    await session.execute(update(AgentPolarRAGInstanceBinding).where(
        AgentPolarRAGInstanceBinding.agent_id == agent_id,
    ).values({
        field: None if body.operation == KnowledgeBindingOperationKind.BIND else "[]",
        f"{body.kb_type.lower()}_excluded_resource_ids_json": None,
    }))
    await _audit_agent(session, admin, "agent_knowledge_binding.visibility", agent_id)
    await session.commit()
    return await get_knowledge_visibility(agent_id, admin, session)


@router.post("/admin/agents/{agent_id}/knowledge-visibility:bind")
async def bind_knowledge_visibility(
    agent_id: str,
    body: KnowledgeVisibilityBindRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await _update_selected_knowledge_visibility(agent_id, body, admin, session, bind=True)


@router.post("/admin/agents/{agent_id}/knowledge-visibility:unbind")
async def unbind_knowledge_visibility(
    agent_id: str,
    body: KnowledgeVisibilityBindRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await _update_selected_knowledge_visibility(agent_id, body, admin, session, bind=False)


async def _update_selected_knowledge_visibility(
    agent_id: str,
    body: KnowledgeVisibilityBindRequest,
    admin: User,
    session: AsyncSession,
    *,
    bind: bool,
):
    await session.execute(update(Agent).where(Agent.id == agent_id).values(knowledge_scope_mode=Agent.knowledge_scope_mode))
    await _agent(session, agent_id)
    pairs = {(target.space_id, target.kb_id) for target in body.targets or []}
    resource_ids = set(body.knowledge_resource_ids or [])
    if pairs:
        candidates = (await session.execute(
            select(KnowledgeResource.id, KnowledgeResource.space_id, KnowledgeResource.kb_id)
            .join(AgentPolarRAGInstanceBinding, (
                AgentPolarRAGInstanceBinding.polarrag_instance_id == KnowledgeResource.polarrag_instance_id
            ) & (AgentPolarRAGInstanceBinding.agent_id == agent_id))
            .where(tuple_(KnowledgeResource.space_id, KnowledgeResource.kb_id).in_(pairs))
        )).all()
        matches: dict[tuple[str, str], list[str]] = {pair: [] for pair in pairs}
        for resource_id, space_id, kb_id in candidates:
            matches[(space_id, kb_id)].append(resource_id)
        if any(len(ids) > 1 for ids in matches.values()):
            raise HTTPException(status_code=409, detail="Ambiguous Space/KB target; use knowledge_resource_ids")
        if any(not ids for ids in matches.values()):
            raise HTTPException(status_code=422, detail="Knowledge resource is outside the Agent's bound instances")
        resource_ids = {ids[0] for ids in matches.values()}
    rows = (await session.execute(
        select(KnowledgeResource, AgentPolarRAGInstanceBinding)
        .join(AgentPolarRAGInstanceBinding, (
            AgentPolarRAGInstanceBinding.polarrag_instance_id == KnowledgeResource.polarrag_instance_id
        ) & (AgentPolarRAGInstanceBinding.agent_id == agent_id))
        .join(PolarRAGSpace, PolarRAGSpace.knowledge_space_id == KnowledgeResource.knowledge_space_id)
        .join(PolarRAGInstance, PolarRAGInstance.id == KnowledgeResource.polarrag_instance_id)
        .where(
            KnowledgeResource.id.in_(resource_ids),
            KnowledgeResource.kb_type.in_(("PUBLIC", "PERSONAL")),
            KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
            KnowledgeResource.enabled.is_(True),
            PolarRAGSpace.enabled.is_(True),
            PolarRAGInstance.status == PolarRAGInstanceStatus.ACTIVE,
            select(EnterpriseIdentitySourceSpaceBinding.id).where(
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == KnowledgeResource.knowledge_space_id,
            ).exists(),
        )
    )).all()
    if {resource.id for resource, _binding in rows} != resource_ids:
        raise HTTPException(status_code=422, detail="Knowledge resource is unavailable or outside the Agent's bound instances and Spaces")
    for resource, binding in rows:
        field = (
            "public_knowledge_resource_ids_json" if resource.kb_type == "PUBLIC"
            else "personal_knowledge_resource_ids_json"
        )
        raw = getattr(binding, field)
        exclusion_field = f"{resource.kb_type.lower()}_excluded_resource_ids_json"
        exclusions = resource_ids_from_json(getattr(binding, exclusion_field) or "[]")
        if bind:
            exclusions.discard(resource.id)
            other_type = "personal" if resource.kb_type == "PUBLIC" else "public"
            other_field = f"{other_type}_excluded_resource_ids_json"
            other_exclusions = resource_ids_from_json(getattr(binding, other_field) or "[]")
            if resource.id in other_exclusions:
                other_exclusions.discard(resource.id)
                setattr(binding, other_field, _encoded_resource_ids(other_exclusions) if other_exclusions else None)
            if raw is not None:
                setattr(binding, field, _encoded_resource_ids(resource_ids_from_json(raw) | {resource.id}))
        elif raw is None:
            # Keep the dynamic default so future synchronized KBs remain eligible.
            exclusions.add(resource.id)
        else:
            setattr(binding, field, _encoded_resource_ids(resource_ids_from_json(raw) - {resource.id}))
        setattr(binding, exclusion_field, _encoded_resource_ids(exclusions) if exclusions else None)
    try:
        await _audit_agent(session, admin, "agent_knowledge_binding.visibility", agent_id)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Knowledge visibility update unavailable") from exc
    visibility = await get_knowledge_visibility(agent_id, admin, session)
    return {**visibility, "agent_id": agent_id, "knowledge_resource_ids": sorted(resource_ids)}


@router.post("/admin/agents/{agent_id}/knowledge-all-users")
async def update_knowledge_all_users(
    agent_id: str,
    body: KnowledgeAllUsersRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    # Serialize policy read-modify-write with subject batches, including SQLite.
    await session.execute(update(Agent).where(Agent.id == agent_id).values(knowledge_scope_mode=Agent.knowledge_scope_mode))
    agent = await _agent(session, agent_id)
    resolved = await _resolve_targets(session, agent, [body.selection_targets()])
    resource_ids = set(resolved.values())
    policies = await knowledge_policies(session, agent_id)
    for resource_id in resource_ids:
        policy = policies.get(resource_id)
        if policy is None:
            policy = AgentKnowledgePolicy(agent_id=agent_id, knowledge_resource_id=resource_id)
            session.add(policy)
        policy.select_all = body.operation == KnowledgeBindingOperationKind.BIND
        policy.excluded_subjects_json = "[]"
    if body.operation == KnowledgeBindingOperationKind.UNBIND:
        for scope in await session.scalars(select(AgentKnowledgeScope).where(
            AgentKnowledgeScope.agent_id == agent_id,
            AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.MANUAL,
        )):
            remaining = resource_ids_from_json(scope.knowledge_resource_ids_json) - resource_ids
            if remaining:
                scope.knowledge_resource_ids_json = _encoded_resource_ids(remaining)
            else:
                await session.execute(delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id == scope.id))
                await session.delete(scope)
    await _audit_agent(session, admin, "agent_knowledge_binding.all_users", agent.id)
    await session.commit()
    return await get_knowledge_all_users_status(
        agent_id, KnowledgeAllUsersStatusRequest(knowledge_resource_ids=sorted(resource_ids)), admin, session,
    )


@router.put("/admin/agents/{agent_id}/knowledge-scope-mode")
async def update_knowledge_scope_mode(
    agent_id: str,
    body: KnowledgeScopeModeRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.api.external_knowledge_scopes import reproject_agent_external_knowledge
    from server.core.external_knowledge_scope_sync import (
        ExternalKnowledgeScopeSyncError,
        clear_agent_external_knowledge,
        lock_agent_knowledge_configuration,
    )

    await lock_agent_knowledge_configuration(session, agent_id)
    agent = await session.get(Agent, agent_id, populate_existing=True)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if body.expected_mode is not None and agent.knowledge_scope_mode != body.expected_mode:
        raise HTTPException(status_code=409, detail="Knowledge scope mode changed; reload before switching")
    if body.mode == AgentKnowledgeScopeMode.LEGACY_ALL and (
        body.external_knowledge_auto_bind_enabled or body.reset_all_users_policies
    ):
        raise HTTPException(status_code=422, detail="Auto-bind and policy reset require SCOPED mode")
    changed = agent.knowledge_scope_mode != body.mode
    removed_policies = 0
    try:
        disabling_auto_bind = (
            body.mode == AgentKnowledgeScopeMode.LEGACY_ALL
            or body.external_knowledge_auto_bind_enabled is False
        )
        clearing = changed or body.reset_all_users_policies or disabling_auto_bind
        removed_scopes = await clear_agent_external_knowledge(session, agent.id) if clearing else 0
        if body.reset_all_users_policies:
            removed_policies = len(list(await session.scalars(select(AgentKnowledgePolicy.knowledge_resource_id).where(
                AgentKnowledgePolicy.agent_id == agent.id,
                AgentKnowledgePolicy.select_all.is_(True),
            ))))
            await session.execute(delete(AgentKnowledgePolicy).where(
                AgentKnowledgePolicy.agent_id == agent.id,
                AgentKnowledgePolicy.select_all.is_(True),
            ))
        # Never materialize LEGACY_ALL implicit defaults as SCOPED grants.
        agent.knowledge_scope_mode = body.mode
        if body.mode == AgentKnowledgeScopeMode.LEGACY_ALL:
            agent.external_knowledge_auto_bind_enabled = False
        elif body.external_knowledge_auto_bind_enabled is not None:
            agent.external_knowledge_auto_bind_enabled = body.external_knowledge_auto_bind_enabled
        await session.flush()
        counts = {}
        if agent.knowledge_scope_mode == AgentKnowledgeScopeMode.SCOPED and agent.external_knowledge_auto_bind_enabled:
            counts = await reproject_agent_external_knowledge(session, agent)
        await _audit_agent(session, admin, "agent_knowledge_scope.mode_update", agent.id)
        await session.commit()
    except ExternalKnowledgeScopeSyncError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Knowledge permission records changed; refresh and retry") from exc
    return {
        "agent_id": agent.id, "mode": agent.knowledge_scope_mode.value,
        "enabled": agent.external_knowledge_auto_bind_enabled,
        "preserved_knowledge_resources": 0,
        "removed_external_scopes": removed_scopes,
        "removed_all_users_policies": removed_policies,
        **counts,
    }


async def _external_scope_source(
    session: AsyncSession,
    source_id: str,
    external_scope_id: str,
) -> tuple[EnterpriseIdentitySource, str]:
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    normalized_scope_id = external_scope_id.strip()
    if not normalized_scope_id or len(normalized_scope_id) > 255:
        raise HTTPException(status_code=422, detail="Invalid external scope id")
    return source, normalized_scope_id


@router.put(
    "/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}",
    response_model=KnowledgeScopeResponse,
)
async def replace_external_knowledge_bindings(
    source_id: str,
    agent_id: str,
    external_scope_id: str,
    body: ExternalKnowledgeBindingRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    source, normalized_scope_id = await _external_scope_source(
        session,
        source_id,
        external_scope_id,
    )
    await _normalize_enterprise_subjects(session, body.subjects)
    resolved_subjects = await _resolve_subjects(
        session,
        agent,
        body.subjects,
        admin.id,
    )
    resolved_targets = await _resolve_targets(session, agent, [body.targets])
    resource_ids = {resolved_targets[target.key()] for target in body.targets}
    scope = (
        await session.execute(
            select(AgentKnowledgeScope).where(
                AgentKnowledgeScope.agent_id == agent.id,
                AgentKnowledgeScope.identity_source_id == source.id,
                AgentKnowledgeScope.external_scope_id == normalized_scope_id,
            )
        )
    ).scalar_one_or_none()
    if scope is None:
        scope = AgentKnowledgeScope(
            id=AgentKnowledgeScope.external_id(
                agent.id,
                source.id,
                normalized_scope_id,
            ),
            agent_id=agent.id,
            name=f"External scope {normalized_scope_id}",
            origin=AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
            identity_source_id=source.id,
            external_scope_id=normalized_scope_id,
            knowledge_resource_ids_json=_encoded_resource_ids(resource_ids),
            created_by_user_id=admin.id,
        )
        session.add(scope)
        await session.flush()
    else:
        scope.knowledge_resource_ids_json = _encoded_resource_ids(resource_ids)
    await _replace_bindings(session, scope, set(resolved_subjects.values()))
    if body.activate_scoped_mode:
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
    await _audit_agent(
        session,
        admin,
        "agent_knowledge_binding.external_replace",
        agent.id,
    )
    await session.commit()
    return await _scope_response(session, scope)


@router.delete(
    "/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}",
    status_code=204,
)
async def delete_external_knowledge_bindings(
    source_id: str,
    agent_id: str,
    external_scope_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    _source, normalized_scope_id = await _external_scope_source(
        session,
        source_id,
        external_scope_id,
    )
    scope = (
        await session.execute(
            select(AgentKnowledgeScope).where(
                AgentKnowledgeScope.agent_id == agent_id,
                AgentKnowledgeScope.identity_source_id == source_id,
                AgentKnowledgeScope.external_scope_id == normalized_scope_id,
            )
        )
    ).scalar_one_or_none()
    if scope is None:
        raise HTTPException(status_code=404, detail="Knowledge scope not found")
    await _audit_agent(
        session,
        admin,
        "agent_knowledge_binding.external_delete",
        agent_id,
    )
    await session.execute(delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id == scope.id))
    await session.delete(scope)
    await session.commit()
    return Response(status_code=204)
