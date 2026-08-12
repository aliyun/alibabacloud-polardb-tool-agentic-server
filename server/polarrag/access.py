from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.models import (
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
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
)


class KnowledgeAccessErrorCode(str, enum.Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NO_ACCESSIBLE_RESOURCE = "NO_ACCESSIBLE_KNOWLEDGE_RESOURCE"
    MIXED_INSTANCE = "MIXED_INSTANCE_NOT_ALLOWED"
    MIXED_SPACE = "MIXED_SPACE_NOT_ALLOWED"
    IDENTITY_CONTEXT_UNAVAILABLE = "IDENTITY_CONTEXT_UNAVAILABLE"


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


async def _domains_with_principals(
    session: AsyncSession,
    user_id: str,
    domains: set[str],
) -> set[str]:
    if not domains:
        return set()
    return set(
        (
            await session.execute(
                select(
                    EnterprisePrincipalAssignment.identity_domain
                ).where(
                    EnterprisePrincipalAssignment.pas_user_id == user_id,
                    EnterprisePrincipalAssignment.identity_domain.in_(domains),
                    EnterprisePrincipalAssignment.status
                    == EnterprisePrincipalStatus.ACTIVE,
                    or_(
                        EnterprisePrincipalAssignment.valid_until.is_(None),
                        EnterprisePrincipalAssignment.valid_until
                        > datetime.now(UTC),
                    ),
                )
            )
        ).scalars()
    )


async def plan_knowledge_access(
    session: AsyncSession,
    user: User,
    knowledge_resource_ids: list[str],
    *,
    allowed_instance_ids: set[str] | None = None,
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
    domains = {resource.identity_domain for resource in by_id.values()}
    available_domains = await _domains_with_principals(
        session,
        user.id,
        domains,
    )
    accessible: list[KnowledgeResource] = []
    partial_failures: list[dict[str, str]] = []
    for resource_id in knowledge_resource_ids:
        resource = by_id.get(resource_id)
        visible = (
            resource is not None
            and (
                allowed_instance_ids is None
                or resource.polarrag_instance_id in allowed_instance_ids
            )
            and resource.enabled
            and resource.sync_status == KnowledgeResourceSyncStatus.ACTIVE
            and resource.space.enabled
            and resource.space.instance.status
            == PolarRAGInstanceStatus.ACTIVE
            and (
                (
                    resource.binding_mode == KnowledgeBindingMode.DOMAIN
                    and resource.identity_domain in available_domains
                )
                or (
                    resource.binding_mode == KnowledgeBindingMode.OWNER
                    and resource.owner_pas_user_id == user.id
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
            space.identity_domain,
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


async def list_visible_knowledge_resources(
    session: AsyncSession,
    user: User,
    *,
    allowed_instance_ids: set[str] | None = None,
) -> list[KnowledgeResource]:
    resources = (
        await session.execute(
            select(KnowledgeResource)
            .options(
                selectinload(KnowledgeResource.space).selectinload(
                    PolarRAGSpace.instance
                )
            )
            .where(
                KnowledgeResource.enabled.is_(True),
                KnowledgeResource.sync_status
                == KnowledgeResourceSyncStatus.ACTIVE,
            )
            .order_by(KnowledgeResource.id)
        )
    ).scalars()
    rows = list(resources)
    domains = {resource.identity_domain for resource in rows}
    available_domains = await _domains_with_principals(
        session,
        user.id,
        domains,
    )
    return [
        resource
        for resource in rows
        if (
            allowed_instance_ids is None
            or resource.polarrag_instance_id in allowed_instance_ids
        )
        and resource.space.enabled
        and resource.space.instance.status == PolarRAGInstanceStatus.ACTIVE
        and (
            (
                resource.binding_mode == KnowledgeBindingMode.DOMAIN
                and resource.identity_domain in available_domains
            )
            or (
                resource.binding_mode == KnowledgeBindingMode.OWNER
                and resource.owner_pas_user_id == user.id
            )
        )
    ]
