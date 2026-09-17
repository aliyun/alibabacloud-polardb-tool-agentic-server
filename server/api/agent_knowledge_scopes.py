from __future__ import annotations

import enum
import json
from datetime import datetime
from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.config import get_config
from server.core.agent_knowledge_scope import (
    resolve_agent_global_resource_scope,
    resource_ids_from_json,
)
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentKnowledgeScopeOrigin,
    AuditStatus,
    Department,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseIdentitySource,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGSpace,
    User,
)


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
    group_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_shape(self) -> KnowledgeBindingSubject:
        populated = {
            "user_id": self.user_id,
            "department_id": self.department_id,
            "identity_source_id": self.identity_source_id,
            "group_id": self.group_id,
        }
        expected = {
            KnowledgeBindingSubjectType.USER: {"user_id"},
            KnowledgeBindingSubjectType.DEPARTMENT: {"department_id"},
            KnowledgeBindingSubjectType.GROUP: {
                "identity_source_id",
                "group_id",
            },
        }[self.type]
        if {key for key, value in populated.items() if value is not None} != expected:
            raise ValueError("subject fields do not match subject type")
        return self

    def key(self) -> tuple[str, ...]:
        if self.type == KnowledgeBindingSubjectType.USER:
            return (self.type.value, self.user_id or "")
        if self.type == KnowledgeBindingSubjectType.DEPARTMENT:
            return (self.type.value, self.department_id or "")
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
    group_assignment_ids: list[str]
    created_at: datetime
    updated_at: datetime | None


class KnowledgeBindingSubjectResponse(BaseModel):
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
    items: list[KnowledgeResourceResponse]
    total: int
    offset: int
    limit: int


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

    global_scope = await resolve_agent_global_resource_scope(session, agent.id)
    resolved: dict[tuple[str, ...], str] = {}
    for key in target_keys:
        if key[0] == "RESOURCE":
            candidates = [resources[key[1]]] if key[1] in resources else []
        else:
            candidates = pair_rows.get((key[1], key[2]), [])
        candidates = [resource for resource in candidates if global_scope.allows(resource)]
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
        if (
            candidate.group_key not in existing_assignments
            and (create_assignment_keys is None or key in create_assignment_keys)
        ):
            session.add(candidate)
            existing_assignments[candidate.group_key] = candidate
    if candidates:
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
    return KnowledgeScopeResponse(
        id=scope.id,
        agent_id=scope.agent_id,
        name=scope.name,
        origin=scope.origin,
        identity_source_id=scope.identity_source_id,
        external_scope_id=scope.external_scope_id,
        knowledge_resource_ids=sorted(resource_ids_from_json(scope.knowledge_resource_ids_json)),
        user_ids=sorted(row.user_id for row in bindings if row.user_id),
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
        )
    return KnowledgeResourceResponse(
        knowledge_resource_id=resource.id,
        space_id=resource.space_id,
        space_name=space_names.get(resource.knowledge_space_id),
        kb_id=resource.kb_id,
        name=resource.name,
    )


def _subject_response(row: Any) -> KnowledgeBindingSubjectResponse:
    if row.user_id is not None:
        return KnowledgeBindingSubjectResponse(
            type=KnowledgeBindingSubjectType.USER,
            user_id=row.user_id,
            display_name=row.user_display_name or row.user_external_id or row.user_id,
            external_id=row.user_external_id,
        )
    if row.group_kind == AgentGroupKind.DEPARTMENT:
        return KnowledgeBindingSubjectResponse(
            type=KnowledgeBindingSubjectType.DEPARTMENT,
            department_id=row.department_id,
            display_name=row.department_name or row.department_id or row.group_assignment_id,
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
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                User.display_name.ilike(pattern),
                User.external_id.ilike(pattern),
                Department.name.ilike(pattern),
                EnterpriseDirectoryGroup.display_name.ilike(pattern),
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
        )
        .join(
            AgentKnowledgeScope,
            AgentKnowledgeScope.id == AgentKnowledgeScopeBinding.scope_id,
        )
        .outerjoin(User, User.id == AgentKnowledgeScopeBinding.user_id)
        .outerjoin(AgentGroupAssignment, group_join)
        .outerjoin(Department, Department.id == AgentGroupAssignment.department_id)
        .outerjoin(EnterpriseDirectoryGroup, directory_group_join)
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
        {
            resource_id
            for row in rows
            for resource_id in resource_ids_from_json(
                row.knowledge_resource_ids_json
            )
        }
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
    return KnowledgeBindingListResponse(
        items=[
            KnowledgeBindingResponse(
                binding_id=row.binding_id,
                scope_id=row.scope_id,
                origin=row.origin,
                identity_source_id=row.identity_source_id,
                external_scope_id=row.external_scope_id,
                subject=_subject_response(row),
                knowledge_resources=[
                    _resource_response(resource_id, resources, space_names)
                    for resource_id in sorted(
                        resource_ids_from_json(row.knowledge_resource_ids_json)
                    )
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


@router.get(
    "/admin/agents/{agent_id}/knowledge-resource-options",
    response_model=KnowledgeResourceListResponse,
)
async def list_knowledge_resource_options(
    agent_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _agent(session, agent_id)
    global_scope = await resolve_agent_global_resource_scope(session, agent_id)
    instance_ids = sorted(global_scope.bindings)
    if not instance_ids:
        return KnowledgeResourceListResponse(items=[], total=0, offset=offset, limit=limit)
    unrestricted_instances = [
        instance_id for instance_id, selected in global_scope.bindings.items() if selected is None
    ]
    selected_public_resources = {
        resource_id for selected in global_scope.bindings.values() if selected is not None for resource_id in selected
    }
    public_filters = [KnowledgeResource.kb_type != "PUBLIC"]
    if unrestricted_instances:
        public_filters.append(KnowledgeResource.polarrag_instance_id.in_(unrestricted_instances))
    if selected_public_resources:
        public_filters.append(KnowledgeResource.id.in_(selected_public_resources))
    filters = [
        KnowledgeResource.polarrag_instance_id.in_(instance_ids),
        KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
        KnowledgeResource.enabled.is_(True),
        PolarRAGSpace.enabled.is_(True),
        or_(*public_filters),
    ]
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                KnowledgeResource.name.ilike(pattern),
                KnowledgeResource.space_id.ilike(pattern),
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
    return KnowledgeResourceListResponse(
        items=[
            KnowledgeResourceResponse(
                knowledge_resource_id=resource.id,
                space_id=resource.space_id,
                space_name=spaces[resource.knowledge_space_id].name,
                kb_id=resource.kb_id,
                name=resource.name,
            )
            for resource in resources
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post("/admin/agents/{agent_id}/knowledge-bindings:batch")
async def update_knowledge_bindings_batch(
    agent_id: str,
    body: KnowledgeBindingBatchRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    all_subjects = [subject for operation in body.operations for subject in operation.subjects]
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
    subject_refs = set(resolved_subjects.values())
    desired = await _selected_manual_resources(session, agent.id, subject_refs)
    for operation in body.operations:
        operation_subjects = {
            resolved_subjects[subject.key()]
            for subject in operation.subjects
            if subject.key() in resolved_subjects
        }
        operation_targets = {resolved_targets[target.key()] for target in operation.targets}
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


@router.put("/admin/agents/{agent_id}/knowledge-scope-mode")
async def update_knowledge_scope_mode(
    agent_id: str,
    body: KnowledgeScopeModeRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _agent(session, agent_id)
    agent.knowledge_scope_mode = body.mode
    await _audit_agent(
        session,
        admin,
        "agent_knowledge_scope.mode_update",
        agent.id,
    )
    await session.commit()
    return {"agent_id": agent.id, "mode": agent.knowledge_scope_mode.value}


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
