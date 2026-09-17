from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from sqlalchemy.orm import selectinload

from server.models import (
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
)
from server.polarrag.identity import (
    IdentityContextUnavailable,
    resolve_acl_context,
    resolve_linked_pas_user_ids,
)


class KnowledgeAccessErrorCode(str, enum.Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NO_ACCESSIBLE_RESOURCE = "NO_ACCESSIBLE_KNOWLEDGE_RESOURCE"
    MIXED_INSTANCE = "MIXED_INSTANCE_NOT_ALLOWED"
    MIXED_SPACE = "MIXED_SPACE_NOT_ALLOWED"
    IDENTITY_CONTEXT_UNAVAILABLE = "IDENTITY_CONTEXT_UNAVAILABLE"
    TOO_MANY_RESOURCES = "TOO_MANY_KNOWLEDGE_RESOURCES"


class KnowledgeAccessError(PermissionError):
    def __init__(self, code: KnowledgeAccessErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class KnowledgeAccessPlan:
    resources: list[KnowledgeResource]
    instance: PolarRAGInstance
    space: PolarRAGSpace
    acl_context: dict[str, Any]
    partial_failures: list[dict[str, str]]


@dataclass(frozen=True)
class ExhaustiveKnowledgeAccessPlan:
    plans: list[KnowledgeAccessPlan]
    partial_failures: list[dict[str, str]]
    requested_count: int


@dataclass(frozen=True)
class KnowledgeResourceScope:
    bindings: dict[str, set[str] | None]
    allowed_resource_ids: set[str] | None = None

    def allows(self, resource: KnowledgeResource) -> bool:
        if (
            self.allowed_resource_ids is not None
            and resource.id not in self.allowed_resource_ids
        ):
            return False
        if resource.polarrag_instance_id not in self.bindings:
            return False
        selected = self.bindings[resource.polarrag_instance_id]
        return (
            resource.kb_type != "PUBLIC"
            or selected is None
            or resource.id in selected
        )


async def _spaces_with_principals(
    session: AsyncSession,
    user_id: str,
    knowledge_space_ids: set[str],
) -> set[str]:
    if not knowledge_space_ids:
        return set()
    available: set[str] = set()
    for knowledge_space_id in knowledge_space_ids:
        try:
            await resolve_acl_context(session, user_id, knowledge_space_id)
        except IdentityContextUnavailable:
            continue
        available.add(knowledge_space_id)
    return available


async def plan_knowledge_access(
    session: AsyncSession,
    user: User,
    knowledge_resource_ids: list[str],
    *,
    resource_scope: KnowledgeResourceScope | None = None,
) -> KnowledgeAccessPlan:
    if not knowledge_resource_ids or len(set(knowledge_resource_ids)) != len(
        knowledge_resource_ids
    ):
        raise KnowledgeAccessError(KnowledgeAccessErrorCode.INVALID_ARGUMENT)
    try:
        for resource_id in knowledge_resource_ids:
            uuid.UUID(resource_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise KnowledgeAccessError(
            KnowledgeAccessErrorCode.INVALID_ARGUMENT
        ) from exc
    rows = (
        await session.execute(
            select(KnowledgeResource)
            .options(
                selectinload(KnowledgeResource.space).selectinload(
                    PolarRAGSpace.instance
                )
            )
            .where(KnowledgeResource.id.in_(knowledge_resource_ids))
        )
    ).scalars()
    by_id = {resource.id: resource for resource in rows}
    available_spaces = await _spaces_with_principals(
        session,
        user.id,
        {resource.knowledge_space_id for resource in by_id.values()},
    )
    linked_owner_ids = {
        knowledge_space_id: await resolve_linked_pas_user_ids(
            session,
            user.id,
            knowledge_space_id,
        )
        for knowledge_space_id in available_spaces
    }
    accessible: list[KnowledgeResource] = []
    partial_failures: list[dict[str, str]] = []
    for resource_id in knowledge_resource_ids:
        resource = by_id.get(resource_id)
        visible = (
            resource is not None
            and (
                resource_scope is None
                or resource_scope.allows(resource)
            )
            and resource.enabled
            and resource.sync_status == KnowledgeResourceSyncStatus.ACTIVE
            and resource.space.enabled
            and resource.space.instance.status
            == PolarRAGInstanceStatus.ACTIVE
            and resource.knowledge_space_id in available_spaces
            and (
                resource.binding_mode == KnowledgeBindingMode.DOMAIN
                or (
                    resource.binding_mode == KnowledgeBindingMode.OWNER
                    and resource.owner_pas_user_id
                    in linked_owner_ids[resource.knowledge_space_id]
                )
            )
        )
        if visible and resource is not None:
            accessible.append(resource)
        else:
            partial_failures.append(
                {
                    "knowledge_resource_id": resource_id,
                    "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
                }
            )
    if not accessible:
        raise KnowledgeAccessError(
            KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE
        )
    instance_ids = {
        resource.polarrag_instance_id for resource in accessible
    }
    if len(instance_ids) != 1:
        raise KnowledgeAccessError(KnowledgeAccessErrorCode.MIXED_INSTANCE)
    space_ids = {resource.space_id for resource in accessible}
    if len(space_ids) != 1:
        raise KnowledgeAccessError(KnowledgeAccessErrorCode.MIXED_SPACE)
    space = accessible[0].space
    try:
        acl_context = await resolve_acl_context(
            session,
            user.id,
            space.knowledge_space_id,
        )
    except IdentityContextUnavailable as exc:
        raise KnowledgeAccessError(
            KnowledgeAccessErrorCode.IDENTITY_CONTEXT_UNAVAILABLE
        ) from exc
    return KnowledgeAccessPlan(
        resources=accessible,
        instance=space.instance,
        space=space,
        acl_context=acl_context,
        partial_failures=partial_failures,
    )


async def plan_exhaustive_knowledge_access(
    session: AsyncSession,
    user: User,
    knowledge_resource_ids: list[str] | None,
    *,
    resource_scope: KnowledgeResourceScope | None = None,
    max_resources: int | None = None,
) -> ExhaustiveKnowledgeAccessPlan:
    if max_resources is None:
        max_resources = (
            get_config()
            .polarrag_tool_limits.max_exhaustive_knowledge_resources
        )
    if knowledge_resource_ids is None:
        resources, total = await list_visible_knowledge_resources_page(
            session,
            user,
            resource_scope=resource_scope,
            offset=0,
            limit=max_resources + 1,
        )
        if total > max_resources:
            raise KnowledgeAccessError(
                KnowledgeAccessErrorCode.TOO_MANY_RESOURCES
            )
        requested_ids = [resource.id for resource in resources]
    else:
        if (
            not knowledge_resource_ids
            or len(set(knowledge_resource_ids)) != len(knowledge_resource_ids)
        ):
            raise KnowledgeAccessError(KnowledgeAccessErrorCode.INVALID_ARGUMENT)
        if len(knowledge_resource_ids) > max_resources:
            raise KnowledgeAccessError(
                KnowledgeAccessErrorCode.TOO_MANY_RESOURCES
            )
        requested_ids = knowledge_resource_ids
    if not requested_ids:
        raise KnowledgeAccessError(
            KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE
        )

    rows = list(
        (
            await session.execute(
                select(KnowledgeResource).where(
                    KnowledgeResource.id.in_(requested_ids)
                )
            )
        ).scalars()
    )
    by_id = {resource.id: resource for resource in rows}
    grouped: dict[tuple[str, str], list[str]] = {}
    partial_failures = [
        {
            "knowledge_resource_id": resource_id,
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        }
        for resource_id in requested_ids
        if resource_id not in by_id
    ]
    for resource_id in requested_ids:
        resource = by_id.get(resource_id)
        if resource is not None:
            grouped.setdefault(
                (resource.polarrag_instance_id, resource.knowledge_space_id),
                [],
            ).append(resource_id)

    plans: list[KnowledgeAccessPlan] = []
    for resource_ids in grouped.values():
        try:
            plan = await plan_knowledge_access(
                session,
                user,
                resource_ids,
                resource_scope=resource_scope,
            )
        except KnowledgeAccessError as exc:
            if exc.code != KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE:
                raise
            partial_failures.extend(
                {
                    "knowledge_resource_id": resource_id,
                    "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
                }
                for resource_id in resource_ids
            )
            continue
        plans.append(plan)
        partial_failures.extend(plan.partial_failures)
    if not plans:
        raise KnowledgeAccessError(
            KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE
        )
    return ExhaustiveKnowledgeAccessPlan(
        plans=plans,
        partial_failures=partial_failures,
        requested_count=len(requested_ids),
    )


async def list_visible_knowledge_resources(
    session: AsyncSession,
    user: User,
    *,
    resource_scope: KnowledgeResourceScope | None = None,
) -> list[KnowledgeResource]:
    rows, _ = await list_visible_knowledge_resources_page(
        session,
        user,
        resource_scope=resource_scope,
        offset=0,
        limit=None,
    )
    return rows


async def list_visible_knowledge_resources_page(
    session: AsyncSession,
    user: User,
    *,
    resource_scope: KnowledgeResourceScope | None = None,
    offset: int = 0,
    limit: int | None = 50,
) -> tuple[list[KnowledgeResource], int]:
    if offset < 0 or limit is not None and limit < 1:
        raise ValueError("offset and limit must be positive")
    filters = await _visible_resource_filters(
        session,
        user,
        resource_scope=resource_scope,
    )
    if filters is None:
        return [], 0
    total = await session.scalar(
        select(func.count(KnowledgeResource.id)).where(*filters)
    )
    query = (
        select(KnowledgeResource)
        .options(
            selectinload(KnowledgeResource.space).selectinload(
                PolarRAGSpace.instance
            )
        )
        .where(*filters)
        .order_by(KnowledgeResource.id)
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = list((await session.execute(query)).scalars())
    return rows, total or 0


async def _visible_resource_filters(
    session: AsyncSession,
    user: User,
    *,
    resource_scope: KnowledgeResourceScope | None,
    knowledge_space_id: str | None = None,
    resource_id: str | None = None,
) -> list[Any] | None:
    candidate_query = (
        select(PolarRAGSpace.knowledge_space_id)
        .join(PolarRAGInstance)
        .where(
            PolarRAGSpace.enabled.is_(True),
            PolarRAGInstance.status == PolarRAGInstanceStatus.ACTIVE,
        )
    )
    if knowledge_space_id is not None:
        candidate_query = candidate_query.where(
            PolarRAGSpace.knowledge_space_id == knowledge_space_id
        )
    candidate_space_ids = set(
        (
            await session.execute(candidate_query)
        ).scalars()
    )
    available_spaces = await _spaces_with_principals(
        session,
        user.id,
        candidate_space_ids,
    )
    if not available_spaces:
        return None
    linked_owner_ids = {
        knowledge_space_id: await resolve_linked_pas_user_ids(
            session,
            user.id,
            knowledge_space_id,
        )
        for knowledge_space_id in available_spaces
    }
    owner_filters = [
        and_(
            KnowledgeResource.knowledge_space_id == knowledge_space_id,
            KnowledgeResource.owner_pas_user_id.in_(owner_ids),
        )
        for knowledge_space_id, owner_ids in linked_owner_ids.items()
        if owner_ids
    ]
    visibility_filter = KnowledgeResource.binding_mode == KnowledgeBindingMode.DOMAIN
    if owner_filters:
        visibility_filter = or_(visibility_filter, *owner_filters)
    filters = [
        KnowledgeResource.enabled.is_(True),
        KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
        KnowledgeResource.knowledge_space_id.in_(available_spaces),
        visibility_filter,
    ]
    if resource_id is not None:
        filters.append(KnowledgeResource.id == resource_id)
    if resource_scope is not None:
        scoped = []
        for instance_id, selected_resource_ids in resource_scope.bindings.items():
            if selected_resource_ids is None:
                scoped.append(KnowledgeResource.polarrag_instance_id == instance_id)
            else:
                scoped.append(
                    and_(
                        KnowledgeResource.polarrag_instance_id == instance_id,
                        or_(
                            KnowledgeResource.kb_type != "PUBLIC",
                            KnowledgeResource.id.in_(selected_resource_ids),
                        ),
                    )
                )
        if not scoped:
            return None
        filters.append(or_(*scoped))
        if resource_scope.allowed_resource_ids is not None:
            if not resource_scope.allowed_resource_ids:
                return None
            filters.append(
                KnowledgeResource.id.in_(resource_scope.allowed_resource_ids)
            )
    return filters


async def list_visible_knowledge_resources_cursor_page(
    session: AsyncSession,
    user: User,
    *,
    resource_scope: KnowledgeResourceScope | None = None,
    knowledge_space_id: str | None = None,
    resource_id: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[KnowledgeResource], bool, bool]:
    if limit < 1:
        raise ValueError("limit must be positive")
    filters = await _visible_resource_filters(
        session,
        user,
        resource_scope=resource_scope,
        knowledge_space_id=knowledge_space_id,
        resource_id=resource_id,
    )
    if filters is None:
        return [], False, cursor is None
    if cursor is not None:
        cursor_exists = await session.scalar(
            select(KnowledgeResource.id)
            .where(*filters, KnowledgeResource.id == cursor)
            .limit(1)
        )
        if cursor_exists is None:
            return [], False, False
    query = (
        select(KnowledgeResource)
        .options(
            selectinload(KnowledgeResource.space).selectinload(
                PolarRAGSpace.instance
            )
        )
        .where(*filters)
        .order_by(KnowledgeResource.id)
        .limit(limit + 1)
    )
    if cursor is not None:
        query = query.where(KnowledgeResource.id > cursor)
    rows = list((await session.execute(query)).scalars())
    return rows[:limit], len(rows) > limit, True
