from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.crypto import decrypt
from server.models import (
    PolarRAGInstance,
    PolarRAGSpace,
    PolarRAGUploadCleanup,
    PolarRAGUploadSession,
    PolarRAGUploadStatus,
)
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import PolarRAGClient, PolarRAGUpstreamError
from server.polarrag.oss import OssObjectStore

logger = logging.getLogger(__name__)

CLEANUP_LEASE = timedelta(minutes=5)
CLEANUP_INTERVAL_SECONDS = 300.0
CLEANUP_LIMIT = 100

CleanupStoreFactory = Callable[
    [PolarRAGSpace | None, "CleanupClaim"], OssObjectStore
]
CleanupClientFactory = Callable[[PolarRAGInstance], PolarRAGClient]
CleanupOperation = Literal[
    "finalizing", "submitting", "reconciling", "cleanup", "aborting"
]


@dataclass(frozen=True)
class CleanupClaim:
    upload_session_id: str
    knowledge_space_id: str
    oss_bucket: str
    oss_endpoint: str
    oss_object_key: str
    oss_multipart_upload_id: str
    oss_access_key_id_ciphertext: str | None
    oss_access_key_secret_ciphertext: str | None
    object_state: str
    expires_at: datetime
    cleanup_attempts: int
    polarrag_instance_id: str | None
    submission_payload_ciphertext: str | None
    reconcile_required: bool
    operation: CleanupOperation
    token: str


def _store_from_cleanup(
    _space: PolarRAGSpace | None,
    cleanup: CleanupClaim,
) -> OssObjectStore:
    if (
        not cleanup.oss_access_key_id_ciphertext
        or not cleanup.oss_access_key_secret_ciphertext
    ):
        raise ValueError("OSS configuration unavailable")
    return OssObjectStore(
        endpoint=cleanup.oss_endpoint,
        bucket=cleanup.oss_bucket,
        access_key_id=decrypt(cleanup.oss_access_key_id_ciphertext),
        access_key_secret=decrypt(
            cleanup.oss_access_key_secret_ciphertext
        ),
    )


async def _claim_cleanup(
    session: AsyncSession,
    cleanup_id: str,
    *,
    now: datetime,
    require_due: bool,
    operation: CleanupOperation,
    polarrag_instance_id: str | None = None,
    submission_payload_ciphertext: str | None = None,
) -> CleanupClaim | None:
    conditions = [
        PolarRAGUploadCleanup.upload_session_id == cleanup_id,
        or_(
            PolarRAGUploadCleanup.cleanup_lease_until.is_(None),
            PolarRAGUploadCleanup.cleanup_lease_until <= now,
        ),
    ]
    if operation in {"cleanup", "aborting"}:
        conditions.append(
            PolarRAGUploadCleanup.reconcile_required.is_(False)
        )
        conditions.append(
            or_(
                PolarRAGUploadCleanup.operation_kind.is_(None),
                ~PolarRAGUploadCleanup.operation_kind.in_(
                    ["submitting", "reconciling"]
                ),
            )
        )
    elif operation == "reconciling":
        conditions.extend(
            [
                PolarRAGUploadCleanup.polarrag_instance_id.is_not(None),
                PolarRAGUploadCleanup.submission_payload_ciphertext.is_not(
                    None
                ),
                or_(
                    PolarRAGUploadCleanup.reconcile_required.is_(True),
                    PolarRAGUploadCleanup.operation_kind == "submitting",
                ),
            ]
        )
    if require_due:
        conditions.append(
            or_(
                PolarRAGUploadCleanup.cleanup_after.is_(None),
                PolarRAGUploadCleanup.cleanup_after <= now,
            )
        )
        if operation != "reconciling":
            conditions.append(PolarRAGUploadCleanup.expires_at <= now)
    lease_until = now + CLEANUP_LEASE
    token = str(uuid4())
    values: dict[str, Any] = {
        "cleanup_lease_until": lease_until,
        "operation_kind": operation,
        "operation_token": token,
    }
    if operation == "submitting":
        if not polarrag_instance_id or not submission_payload_ciphertext:
            raise ValueError("submission snapshot is required")
        values.update(
            polarrag_instance_id=polarrag_instance_id,
            submission_payload_ciphertext=submission_payload_ciphertext,
            reconcile_required=False,
        )
    elif operation == "reconciling":
        values["reconcile_required"] = True
    claimed = await session.execute(
        update(PolarRAGUploadCleanup)
        .where(*conditions)
        .values(**values)
        .execution_options(synchronize_session="fetch")
    )
    if getattr(claimed, "rowcount", 0) != 1:
        await session.rollback()
        return None
    cleanup = (
        await session.execute(
            select(PolarRAGUploadCleanup)
            .where(PolarRAGUploadCleanup.upload_session_id == cleanup_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    result = CleanupClaim(
        upload_session_id=cleanup.upload_session_id,
        knowledge_space_id=cleanup.knowledge_space_id,
        oss_bucket=cleanup.oss_bucket,
        oss_endpoint=cleanup.oss_endpoint,
        oss_object_key=cleanup.oss_object_key,
        oss_multipart_upload_id=cleanup.oss_multipart_upload_id,
        oss_access_key_id_ciphertext=(
            cleanup.oss_access_key_id_ciphertext
        ),
        oss_access_key_secret_ciphertext=(
            cleanup.oss_access_key_secret_ciphertext
        ),
        object_state=cleanup.object_state,
        expires_at=cleanup.expires_at,
        cleanup_attempts=cleanup.cleanup_attempts,
        polarrag_instance_id=cleanup.polarrag_instance_id,
        submission_payload_ciphertext=(
            cleanup.submission_payload_ciphertext
        ),
        reconcile_required=cleanup.reconcile_required,
        operation=operation,
        token=token,
    )
    await session.commit()
    return result


async def _release_cleanup(
    session: AsyncSession,
    claim: CleanupClaim,
    *,
    reconcile_required: bool | None = None,
) -> bool:
    values: dict[str, Any] = {
        "cleanup_lease_until": None,
        "operation_kind": None,
        "operation_token": None,
    }
    if reconcile_required is not None:
        values["reconcile_required"] = reconcile_required
    released = await session.execute(
        update(PolarRAGUploadCleanup)
        .where(
            PolarRAGUploadCleanup.upload_session_id
            == claim.upload_session_id,
            PolarRAGUploadCleanup.operation_kind == claim.operation,
            PolarRAGUploadCleanup.operation_token == claim.token,
        )
        .values(**values)
        .execution_options(synchronize_session="fetch")
    )
    await session.commit()
    return getattr(released, "rowcount", 0) == 1


async def _claimed_cleanup(
    session: AsyncSession,
    claim: CleanupClaim,
) -> PolarRAGUploadCleanup | None:
    return (
        await session.execute(
            select(PolarRAGUploadCleanup)
            .where(
                PolarRAGUploadCleanup.upload_session_id
                == claim.upload_session_id,
                PolarRAGUploadCleanup.operation_kind == claim.operation,
                PolarRAGUploadCleanup.operation_token == claim.token,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _clear_claim(cleanup: PolarRAGUploadCleanup) -> None:
    cleanup.cleanup_lease_until = None
    cleanup.operation_kind = None
    cleanup.operation_token = None
    cleanup.reconcile_required = False


def is_definitive_rejection(exc: PolarRAGUpstreamError) -> bool:
    return (
        exc.status_code is not None
        and 400 <= exc.status_code < 500
        and exc.status_code not in {408, 425, 429}
        and not exc.retryable
    )


async def _cleanup_object(
    store: OssObjectStore,
    cleanup: CleanupClaim,
) -> None:
    if cleanup.object_state in {"multipart", "finalizing"}:
        await store.abort_multipart(
            cleanup.oss_object_key,
            cleanup.oss_multipart_upload_id,
        )
    if cleanup.object_state in {"object", "finalizing"}:
        await store.delete(cleanup.oss_object_key)


def _submission_request(
    claim: CleanupClaim,
) -> tuple[str, str, dict[str, Any]]:
    if not claim.submission_payload_ciphertext:
        raise ValueError("submission snapshot unavailable")
    payload = json.loads(decrypt(claim.submission_payload_ciphertext))
    if not isinstance(payload, dict):
        raise ValueError("submission snapshot invalid")
    space_id = payload.pop("space_id", None)
    kb_id = payload.pop("kb_id", None)
    if not isinstance(space_id, str) or not isinstance(kb_id, str):
        raise ValueError("submission snapshot invalid")
    required = {
        "oss_path": str,
        "filename": str,
        "file_type": str,
        "file_md5": str,
        "file_size_bytes": int,
        "metadata": dict,
        "acl_context": dict,
    }
    if any(
        not isinstance(payload.get(name), expected)
        for name, expected in required.items()
    ):
        raise ValueError("submission snapshot invalid")
    return space_id, kb_id, payload


async def _schedule_retry(
    session: AsyncSession,
    claim: CleanupClaim,
    *,
    now: datetime,
    error_code: str,
    reconcile_required: bool,
) -> None:
    attempts = claim.cleanup_attempts + 1
    delay = min(60 * (2 ** min(attempts - 1, 6)), 3600)
    await session.execute(
        update(PolarRAGUploadCleanup)
        .where(
            PolarRAGUploadCleanup.upload_session_id
            == claim.upload_session_id,
            PolarRAGUploadCleanup.operation_kind == claim.operation,
            PolarRAGUploadCleanup.operation_token == claim.token,
        )
        .values(
            cleanup_attempts=attempts,
            cleanup_after=now + timedelta(seconds=delay),
            cleanup_lease_until=None,
            operation_kind=None,
            operation_token=None,
            reconcile_required=reconcile_required,
            cleanup_error_code=error_code,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    logger.warning(
        "polarrag.upload.cleanup_retry_scheduled",
        extra={
            "upload_session_id": claim.upload_session_id,
            "error_code": error_code,
        },
    )


async def _finish_claim(
    session: AsyncSession,
    claim: CleanupClaim,
    *,
    now: datetime,
    document: dict[str, Any] | None,
) -> bool:
    cleanup = await _claimed_cleanup(session, claim)
    if cleanup is None:
        await session.rollback()
        return False
    upload = await session.get(PolarRAGUploadSession, claim.upload_session_id)
    if upload is not None:
        upload.completed_at = now
        if document is None:
            upload.status = PolarRAGUploadStatus.ABORTED
        else:
            upload.status = PolarRAGUploadStatus.COMPLETED
            upload.doc_id = document["doc_id"]
            upload.upstream_status = document.get("status", "DISPATCHED")
    await session.delete(cleanup)
    await session.commit()
    return True


async def sweep_expired_uploads(
    session_factory,
    *,
    now: datetime | None = None,
    limit: int = CLEANUP_LIMIT,
    object_store_factory: CleanupStoreFactory = _store_from_cleanup,
    client_factory: CleanupClientFactory | None = None,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    async with session_factory() as session:
        cleanup_ids = list(
            (
                await session.execute(
                    select(PolarRAGUploadCleanup.upload_session_id)
                    .where(
                        or_(
                            PolarRAGUploadCleanup.expires_at <= current,
                            PolarRAGUploadCleanup.reconcile_required.is_(True),
                            PolarRAGUploadCleanup.operation_kind
                            == "submitting",
                        ),
                        or_(
                            PolarRAGUploadCleanup.cleanup_after.is_(None),
                            PolarRAGUploadCleanup.cleanup_after <= current,
                        ),
                        or_(
                            PolarRAGUploadCleanup.cleanup_lease_until.is_(None),
                            PolarRAGUploadCleanup.cleanup_lease_until
                            <= current,
                        ),
                    )
                    .order_by(PolarRAGUploadCleanup.expires_at)
                    .limit(limit)
                )
            ).scalars()
        )

    result = {"cleaned": 0, "failed": 0}
    for cleanup_id in cleanup_ids:
        async with session_factory() as session:
            reconcile_claim = await _claim_cleanup(
                session,
                cleanup_id,
                now=current,
                require_due=True,
                operation="reconciling",
            )
            if reconcile_claim is not None:
                instance = await session.get(
                    PolarRAGInstance,
                    reconcile_claim.polarrag_instance_id,
                )
                await session.commit()
                try:
                    if instance is None:
                        raise ValueError("PolarRAG instance unavailable")
                    space_id, kb_id, payload = _submission_request(
                        reconcile_claim
                    )
                    document = await (
                        client_factory or client_from_instance
                    )(instance).submit_document(space_id, kb_id, **payload)
                except Exception:
                    await _schedule_retry(
                        session,
                        reconcile_claim,
                        now=current,
                        error_code="POLARRAG_RECONCILE_FAILED",
                        reconcile_required=True,
                    )
                    result["failed"] += 1
                    continue
                if await _finish_claim(
                    session,
                    reconcile_claim,
                    now=current,
                    document=document,
                ):
                    result["cleaned"] += 1
                continue

            claim = await _claim_cleanup(
                session,
                cleanup_id,
                now=current,
                require_due=True,
                operation="cleanup",
            )
            if claim is None:
                continue
            upload = await session.get(PolarRAGUploadSession, cleanup_id)
            if (
                upload is not None
                and upload.status == PolarRAGUploadStatus.COMPLETED
            ):
                cleanup = await _claimed_cleanup(session, claim)
                if cleanup is not None:
                    await session.delete(cleanup)
                    await session.commit()
                    result["cleaned"] += 1
                else:
                    await session.rollback()
                continue
            space = await session.get(
                PolarRAGSpace, claim.knowledge_space_id
            )
            await session.commit()
            try:
                store = object_store_factory(space, claim)
                await _cleanup_object(store, claim)
            except Exception:
                await _schedule_retry(
                    session,
                    claim,
                    now=current,
                    error_code="OSS_CLEANUP_FAILED",
                    reconcile_required=False,
                )
                result["failed"] += 1
                continue
            if await _finish_claim(
                session, claim, now=current, document=None
            ):
                result["cleaned"] += 1
    return result


async def upload_cleanup_loop(
    session_factory,
    *,
    interval_seconds: float = CLEANUP_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            await sweep_expired_uploads(session_factory)
        except Exception:
            logger.exception("PolarRAG upload cleanup sweep failed")
        await asyncio.sleep(interval_seconds)
