from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.models import (
    EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGSpace,
    User,
    UserStatus,
)
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import (
    PolarRAGClient,
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGUpstreamError,
)

logger = logging.getLogger(__name__)


async def _resolve_owner(
    session: AsyncSession,
    record: PolarRAGKnowledgeBaseRecord,
    now: datetime,
) -> str | None:
    owner = record.owner
    if (
        not isinstance(owner, dict)
        or owner.get("type") != EnterprisePrincipalType.USER.value
        or not isinstance(owner.get("provider"), str)
        or not isinstance(owner.get("id"), str)
    ):
        return None
    if owner["provider"] == "polarrag":
        has_membership = exists(
            select(EnterprisePrincipalAssignment.id).where(
                EnterprisePrincipalAssignment.pas_user_id == User.id,
                EnterprisePrincipalAssignment.identity_domain
                == record.identity_domain,
                EnterprisePrincipalAssignment.status
                == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > now,
                ),
            )
        )
        return (
            await session.execute(
                select(User.id).where(
                    User.external_id == owner["id"],
                    User.status == UserStatus.ACTIVE,
                    has_membership,
                )
            )
        ).scalar_one_or_none()
    if owner["provider"] not in EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS:
        return None
    return (
        await session.execute(
            select(EnterprisePrincipalAssignment.pas_user_id).where(
                EnterprisePrincipalAssignment.identity_domain
                == record.identity_domain,
                EnterprisePrincipalAssignment.provider == owner["provider"],
                EnterprisePrincipalAssignment.principal_type
                == EnterprisePrincipalType.USER,
                EnterprisePrincipalAssignment.principal_id == owner["id"],
                EnterprisePrincipalAssignment.status
                == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > now,
                ),
            )
        )
    ).scalar_one_or_none()


async def sync_space_catalog(
    session: AsyncSession,
    space: PolarRAGSpace,
    client: PolarRAGClient,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    try:
        records = await client.list_knowledge_bases(space.space_id)
    except PolarRAGUpstreamError as exc:
        if exc.status_code == 409:
            space.enabled = False
            space.last_synced_at = current
            await session.execute(
                update(KnowledgeResource)
                .where(
                    KnowledgeResource.polarrag_instance_id
                    == space.polarrag_instance_id,
                    KnowledgeResource.space_id == space.space_id,
                )
                .values(
                    enabled=False,
                    sync_status=KnowledgeResourceSyncStatus.UPSTREAM_DISABLED,
                )
            )
            if commit:
                await session.commit()
            else:
                await session.flush()
        raise
    existing = {
        resource.kb_id: resource
        for resource in (
            await session.execute(
                select(KnowledgeResource).where(
                    KnowledgeResource.polarrag_instance_id
                    == space.polarrag_instance_id,
                    KnowledgeResource.space_id == space.space_id,
                )
            )
        ).scalars()
    }
    seen: set[str] = set()
    counts = {"active": 0, "disabled": 0, "owner_unresolved": 0}
    for record in records:
        if (
            record.space_id != space.space_id
            or record.identity_domain != space.identity_domain
        ):
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.INVALID_RESPONSE
            )
        seen.add(record.kb_id)
        resource = existing.get(record.kb_id)
        if resource is None:
            resource = KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=space.polarrag_instance_id,
                space_id=space.space_id,
                kb_id=record.kb_id,
                name=record.name,
                kb_type=record.kb_type.upper(),
                identity_domain=space.identity_domain,
                sync_status=KnowledgeResourceSyncStatus.UPSTREAM_DISABLED,
                enabled=False,
            )
            session.add(resource)
        resource.name = record.name
        resource.usage = record.usage
        resource.kb_type = record.kb_type.upper()
        resource.identity_domain = space.identity_domain
        resource.upstream_updated_at = record.updated_at
        resource.owner_pas_user_id = None
        if resource.kb_type == "PUBLIC":
            resource.binding_mode = KnowledgeBindingMode.DOMAIN
            resource.sync_status = KnowledgeResourceSyncStatus.ACTIVE
            resource.enabled = True
            counts["active"] += 1
        elif resource.kb_type == "PERSONAL":
            owner_id = await _resolve_owner(session, record, current)
            resource.binding_mode = KnowledgeBindingMode.OWNER
            resource.owner_pas_user_id = owner_id
            if owner_id is None:
                resource.sync_status = KnowledgeResourceSyncStatus.OWNER_UNRESOLVED
                resource.enabled = False
                counts["owner_unresolved"] += 1
            else:
                resource.sync_status = KnowledgeResourceSyncStatus.ACTIVE
                resource.enabled = True
                counts["active"] += 1
        else:
            resource.binding_mode = None
            resource.sync_status = (
                KnowledgeResourceSyncStatus.UNSUPPORTED_KB_TYPE
            )
            resource.enabled = False
            counts["disabled"] += 1
    for kb_id, resource in existing.items():
        if kb_id not in seen:
            resource.enabled = False
            resource.sync_status = KnowledgeResourceSyncStatus.UPSTREAM_DISABLED
            counts["disabled"] += 1
    counts["knowledge_bases"] = len(set(existing) | seen)
    space.last_synced_at = current
    if commit:
        await session.commit()
    else:
        await session.flush()
    return counts


async def sync_enabled_spaces_once(
    session_factory,
    *,
    client_factory: Any = client_from_instance,
) -> None:
    async with session_factory() as session:
        spaces = list(
            (
                await session.execute(
                    select(PolarRAGSpace)
                    .options(selectinload(PolarRAGSpace.instance))
                    .where(PolarRAGSpace.enabled.is_(True))
                    .order_by(PolarRAGSpace.knowledge_space_id)
                )
            ).scalars()
        )
        for space in spaces:
            instance_id = space.polarrag_instance_id
            knowledge_space_id = space.knowledge_space_id
            try:
                await sync_space_catalog(
                    session,
                    space,
                    client_factory(space.instance),
                )
            except Exception as exc:
                await session.rollback()
                logger.warning(
                    "polarrag.catalog.sync_failed",
                    extra={
                        "polarrag_instance_id": instance_id,
                        "knowledge_space_id": knowledge_space_id,
                        "error_type": type(exc).__name__,
                    },
                )


async def catalog_sync_loop(
    session_factory,
    *,
    interval_seconds: float = 300.0,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await sync_enabled_spaces_once(session_factory)
