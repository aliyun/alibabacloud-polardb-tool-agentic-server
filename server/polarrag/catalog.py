from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
    UserStatus,
)
from server.enterprise_identity.service import identity_provider_key
from server.polarrag.identity import IdentityContextUnavailable, resolve_acl_context
from server.polarrag.client import _MAX_CATALOG_PAGES, client_from_instance
from server.polarrag.contracts import (
    PolarRAGClient,
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGUpstreamError,
)

logger = logging.getLogger(__name__)

_CATALOG_SYNC_PAGE_SIZE = 100
_CATALOG_SYNC_LEASE_SECONDS = 120
_CATALOG_SYNC_LEASE_RENEW_SECONDS = 30


async def _renew_space_catalog_sync_lease(
    session_factory: Any,
    knowledge_space_id: str,
    worker_id: str,
) -> None:
    while True:
        await asyncio.sleep(_CATALOG_SYNC_LEASE_RENEW_SECONDS)
        now = datetime.now(UTC)
        async with session_factory() as session:
            renewed = await session.execute(
                update(PolarRAGSpace)
                .where(
                    PolarRAGSpace.knowledge_space_id == knowledge_space_id,
                    PolarRAGSpace.catalog_sync_worker_id == worker_id,
                    PolarRAGSpace.catalog_sync_status == "running",
                )
                .values(
                    catalog_sync_lease_until=now
                    + timedelta(seconds=_CATALOG_SYNC_LEASE_SECONDS)
                )
            )
            if renewed.rowcount != 1:
                await session.rollback()
                return
            await session.commit()


async def claim_space_catalog_sync(
    session: AsyncSession,
    knowledge_space_id: str,
    worker_id: str,
) -> bool:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "catalog_sync_status": "running",
        "catalog_sync_result_json": None,
        "catalog_sync_error": None,
        "catalog_sync_worker_id": worker_id,
        "catalog_sync_lease_until": now + timedelta(seconds=_CATALOG_SYNC_LEASE_SECONDS),
    }
    claimed = await session.execute(
        update(PolarRAGSpace)
        .where(
            PolarRAGSpace.knowledge_space_id == knowledge_space_id,
            PolarRAGSpace.enabled.is_(True),
            or_(
                PolarRAGSpace.catalog_sync_worker_id.is_(None),
                PolarRAGSpace.catalog_sync_lease_until.is_(None),
                PolarRAGSpace.catalog_sync_lease_until <= now,
            ),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    await session.commit()
    return True


async def run_claimed_space_catalog_sync(
    session_factory: Any,
    knowledge_space_id: str,
    worker_id: str,
    *,
    client_factory: Any = client_from_instance,
) -> dict[str, int] | None:
    renewer = asyncio.create_task(
        _renew_space_catalog_sync_lease(
            session_factory, knowledge_space_id, worker_id
        )
    )
    try:
        return await _run_claimed_space_catalog_sync(
            session_factory,
            knowledge_space_id,
            worker_id,
            client_factory=client_factory,
        )
    finally:
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)


async def _run_claimed_space_catalog_sync(
    session_factory: Any,
    knowledge_space_id: str,
    worker_id: str,
    *,
    client_factory: Any,
) -> dict[str, int] | None:
    async with session_factory() as session:
        space = await session.get(PolarRAGSpace, knowledge_space_id)
        if space is None or space.catalog_sync_worker_id != worker_id:
            return None
        instance = space.instance
        try:
            result = await sync_space_catalog(
                session,
                space,
                client_factory(instance),
                commit=False,
            )
        except PolarRAGUpstreamError as exc:
            if exc.status_code != 409:
                await session.rollback()
            failed = await session.execute(
                update(PolarRAGSpace)
                .where(
                    PolarRAGSpace.knowledge_space_id == knowledge_space_id,
                    PolarRAGSpace.catalog_sync_worker_id == worker_id,
                )
                .values(
                    catalog_sync_status="failed",
                    catalog_sync_error=type(exc).__name__,
                    catalog_sync_worker_id=None,
                    catalog_sync_lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            if failed.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                return None
            await session.commit()
            raise
        except Exception as exc:
            await session.rollback()
            failed = await session.execute(
                update(PolarRAGSpace)
                .where(
                    PolarRAGSpace.knowledge_space_id == knowledge_space_id,
                    PolarRAGSpace.catalog_sync_worker_id == worker_id,
                )
                .values(
                    catalog_sync_status="failed",
                    catalog_sync_error=type(exc).__name__,
                    catalog_sync_worker_id=None,
                    catalog_sync_lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            if failed.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                return None
            await session.commit()
            raise
        completed = await session.execute(
            update(PolarRAGSpace)
            .where(
                PolarRAGSpace.knowledge_space_id == knowledge_space_id,
                PolarRAGSpace.catalog_sync_worker_id == worker_id,
            )
            .values(
                catalog_sync_status="completed",
                catalog_sync_result_json=json.dumps(result, sort_keys=True),
                catalog_sync_error=None,
                catalog_sync_worker_id=None,
                catalog_sync_lease_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        if completed.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return None
        await session.commit()
        return result


async def _read_catalog_page(
    client: PolarRAGClient,
    space_id: str,
    cursor: str | None,
) -> tuple[list[PolarRAGKnowledgeBaseRecord], str | None]:
    return await client.list_knowledge_bases_page(
        space_id,
        cursor=cursor,
        page_size=_CATALOG_SYNC_PAGE_SIZE,
    )


async def _resolve_owner(
    session: AsyncSession,
    space: PolarRAGSpace,
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
        users = list(
            (
                await session.execute(
                    select(User).where(
                        User.external_id == owner["id"],
                        User.status == UserStatus.ACTIVE,
                    )
                )
            ).scalars()
        )
        for user in users:
            if (
                await session.execute(
                    select(User.id).where(User.id == user.id, has_membership)
                )
            ).scalar_one_or_none() is not None:
                return user.id
            try:
                context = await resolve_acl_context(
                    session,
                    user.id,
                    space.knowledge_space_id,
                    now=now,
                )
            except IdentityContextUnavailable:
                continue
            if {"provider": "polarrag", "type": "user", "id": owner["id"]} in context["principals"]:
                return user.id
        return None
    if owner["provider"] not in EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS:
        return None
    mapped_owner = (
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
    if mapped_owner is not None:
        return mapped_owner
    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource)
                .join(
                    EnterpriseIdentitySourceSpaceBinding,
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id
                    == EnterpriseIdentitySource.id,
                )
                .where(
                    EnterpriseIdentitySourceSpaceBinding.knowledge_space_id
                    == space.knowledge_space_id,
                    EnterpriseIdentitySource.status
                    == EnterpriseIdentitySourceStatus.ACTIVE,
                    EnterpriseIdentitySource.provider == owner["provider"],
                )
            )
        ).scalars()
    )
    owner_ids: set[str] = set()
    for source in sources:
        candidate = (
            await session.execute(
                select(UserExternalIdentity.user_id)
                .join(
                    EnterpriseDirectoryUser,
                    EnterpriseDirectoryUser.external_user_id
                    == UserExternalIdentity.external_subject,
                )
                .where(
                    UserExternalIdentity.identity_provider == identity_provider_key(source),
                    UserExternalIdentity.external_subject == owner["id"],
                    EnterpriseDirectoryUser.identity_source_id == source.id,
                    EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if candidate is not None:
            owner_ids.add(candidate)
    verified_owner_ids: set[str] = set()
    expected_principal = {
        "provider": owner["provider"],
        "type": "user",
        "id": owner["id"],
    }
    for owner_id in owner_ids:
        try:
            context = await resolve_acl_context(
                session,
                owner_id,
                space.knowledge_space_id,
                now=now,
            )
        except IdentityContextUnavailable:
            continue
        if expected_principal in context["principals"]:
            verified_owner_ids.add(owner_id)
    return next(iter(verified_owner_ids)) if len(verified_owner_ids) == 1 else None


async def sync_space_catalog(
    session: AsyncSession,
    space: PolarRAGSpace,
    client: PolarRAGClient,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    sync_token = str(uuid.uuid4())
    try:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_kb_ids: set[str] = set()
        counts = {"active": 0, "disabled": 0, "owner_unresolved": 0}
        for _page_number in range(_MAX_CATALOG_PAGES):
            records, next_cursor = await _read_catalog_page(client, space.space_id, cursor)
            for record in records:
                if record.kb_id in seen_kb_ids:
                    raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
                seen_kb_ids.add(record.kb_id)
            kb_ids = [record.kb_id for record in records]
            existing = {
                resource.kb_id: resource
                for resource in (
                    await session.execute(
                        select(KnowledgeResource).where(
                            KnowledgeResource.polarrag_instance_id
                            == space.polarrag_instance_id,
                            KnowledgeResource.space_id == space.space_id,
                            KnowledgeResource.kb_id.in_(kb_ids),
                        )
                    )
                ).scalars()
            }
            for record in records:
                if (
                    record.space_id != space.space_id
                    or record.identity_domain != space.identity_domain
                ):
                    raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
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
                resource.catalog_sync_token = sync_token
                resource.owner_pas_user_id = None
                if resource.kb_type == "PUBLIC":
                    resource.binding_mode = KnowledgeBindingMode.DOMAIN
                    resource.sync_status = KnowledgeResourceSyncStatus.ACTIVE
                    resource.enabled = True
                    counts["active"] += 1
                elif resource.kb_type == "PERSONAL":
                    owner_id = await _resolve_owner(session, space, record, current)
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
            await session.flush()
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
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
        elif commit:
            await session.rollback()
        raise
    disabled = await session.execute(
        update(KnowledgeResource)
        .where(
            KnowledgeResource.polarrag_instance_id == space.polarrag_instance_id,
            KnowledgeResource.space_id == space.space_id,
            or_(
                KnowledgeResource.catalog_sync_token != sync_token,
                KnowledgeResource.catalog_sync_token.is_(None),
            ),
        )
        .values(
            enabled=False,
            sync_status=KnowledgeResourceSyncStatus.UPSTREAM_DISABLED,
        )
    )
    counts["disabled"] += disabled.rowcount or 0  # type: ignore[attr-defined]
    counts["knowledge_bases"] = (
        await session.scalar(
            select(func.count(KnowledgeResource.id)).where(
                KnowledgeResource.polarrag_instance_id == space.polarrag_instance_id,
                KnowledgeResource.space_id == space.space_id,
            )
        )
        or 0
    )
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
        space_ids = list(
            (
                await session.execute(
                    select(PolarRAGSpace.knowledge_space_id)
                    .where(PolarRAGSpace.enabled.is_(True))
                    .order_by(PolarRAGSpace.knowledge_space_id)
                )
            ).scalars()
        )
    for knowledge_space_id in space_ids:
        worker_id = str(uuid.uuid4())
        async with session_factory() as session:
            if not await claim_space_catalog_sync(
                session, knowledge_space_id, worker_id
            ):
                continue
        try:
            await run_claimed_space_catalog_sync(
                session_factory,
                knowledge_space_id,
                worker_id,
                client_factory=client_factory,
            )
        except Exception as exc:
            logger.warning(
                "polarrag.catalog.sync_failed",
                extra={
                    "knowledge_space_id": knowledge_space_id,
                    "error_type": type(exc).__name__,
                },
            )


async def catalog_sync_loop(
    session_factory,
    *,
    interval_seconds: float = 300.0,
    feature=None,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        if feature is None:
            await sync_enabled_spaces_once(session_factory)
        else:
            from server.features.knowledge import KnowledgeUnavailable
            try:
                async with feature.operation("catalog_sync"):
                    await sync_enabled_spaces_once(session_factory)
            except KnowledgeUnavailable:
                continue
