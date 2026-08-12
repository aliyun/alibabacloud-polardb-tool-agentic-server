from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    KnowledgeResource,
    PolarRAGSpace,
    PolarRAGUploadSession,
    PolarRAGUploadStatus,
    User,
    UserRole,
)
from server.polarrag.access import (
    KnowledgeAccessError,
    KnowledgeAccessPlan,
    plan_knowledge_access,
)
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import (
    PolarRAGClient,
    PolarRAGUpstreamError,
)
from server.polarrag.oss import OssObjectStore
from server.polarrag.upload import (
    MAX_UPLOAD_BYTES,
    document_actor,
    document_object_key,
    file_type_from_filename,
    new_upload_object_id,
    object_store_from_space,
    validate_filename,
)

PART_SIZE_BYTES = 8 * 1024 * 1024
SIGNED_URL_TTL_SECONDS = 15 * 60
UPLOAD_SESSION_TTL = timedelta(hours=24)
_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

ObjectStoreFactory = Callable[[PolarRAGSpace], OssObjectStore]
ClientFactory = Callable[[Any], PolarRAGClient]


class UploadSessionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _part_uploads(
    store: OssObjectStore,
    row: PolarRAGUploadSession,
    part_numbers: Sequence[int],
) -> list[dict[str, Any]]:
    try:
        return [
            {
                "part_number": number,
                "upload_url": store.sign_part_url(
                    row.oss_object_key,
                    row.oss_multipart_upload_id,
                    number,
                    expires_seconds=SIGNED_URL_TTL_SECONDS,
                ),
            }
            for number in part_numbers
        ]
    except Exception as exc:
        raise UploadSessionError("OSS_UPLOAD_FAILED") from exc


def _response(
    row: PolarRAGUploadSession,
    *,
    parts: list[dict[str, Any]],
    uploaded_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "upload_session_id": row.id,
        "upload_mode": "multipart",
        "status": row.status.value,
        "part_size_bytes": row.part_size_bytes,
        "part_count": row.part_count,
        "parts": parts,
        "uploaded_parts": uploaded_parts or [],
        "upload_urls_expire_in_seconds": SIGNED_URL_TTL_SECONDS,
        "session_expires_at": _as_utc(row.expires_at).isoformat(),
    }


def _validate_metadata(
    *,
    filename: str,
    file_size_bytes: int,
    file_md5: str,
    file_sha256: str,
    content_type: str | None,
) -> tuple[str, str, str, str, str | None]:
    try:
        normalized_filename = validate_filename(filename)
        file_type = file_type_from_filename(normalized_filename)
    except ValueError as exc:
        raise UploadSessionError("INVALID_ARGUMENT") from exc
    if (
        isinstance(file_size_bytes, bool)
        or file_size_bytes < 1
        or file_size_bytes > MAX_UPLOAD_BYTES
        or not _MD5.fullmatch(file_md5)
        or not _SHA256.fullmatch(file_sha256)
    ):
        raise UploadSessionError("INVALID_ARGUMENT")
    normalized_content_type = content_type.strip() if content_type else None
    if normalized_content_type and (
        len(normalized_content_type) > 255
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized_content_type
        )
    ):
        raise UploadSessionError("INVALID_ARGUMENT")
    return (
        normalized_filename,
        file_type,
        file_md5.lower(),
        file_sha256.lower(),
        normalized_content_type,
    )


def _require_upload_ready(space: PolarRAGSpace) -> None:
    if (
        not space.oss_config_validated
        or not space.oss_bucket
        or not space.oss_endpoint
        or not space.oss_object_prefix
        or not space.oss_access_key_id_ciphertext
        or not space.oss_access_key_secret_ciphertext
    ):
        raise UploadSessionError("POLARRAG_OSS_NOT_CONFIGURED")


def _store_for_space(
    space: PolarRAGSpace,
    factory: ObjectStoreFactory,
    row: PolarRAGUploadSession | None = None,
) -> OssObjectStore:
    _require_upload_ready(space)
    if row is not None and (
        row.oss_bucket != space.oss_bucket
        or row.oss_endpoint != space.oss_endpoint
    ):
        raise UploadSessionError("POLARRAG_OSS_CONFIGURATION_CHANGED")
    try:
        return factory(space)
    except Exception as exc:
        raise UploadSessionError("POLARRAG_OSS_NOT_CONFIGURED") from exc


async def _owned_session(
    session: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
    upload_session_id: str,
) -> PolarRAGUploadSession:
    row = (
        await session.execute(
            select(PolarRAGUploadSession)
            .where(
                PolarRAGUploadSession.id == upload_session_id,
                PolarRAGUploadSession.pas_user_id == user_id,
                PolarRAGUploadSession.agent_id == agent_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACCESSIBLE")
    return row


async def _session_access(
    session: AsyncSession,
    user: User,
    row: PolarRAGUploadSession,
    *,
    allowed_instance_ids: set[str] | None,
) -> KnowledgeAccessPlan:
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            [row.knowledge_resource_id],
            allowed_instance_ids=allowed_instance_ids,
        )
    except KnowledgeAccessError as exc:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACCESSIBLE") from exc
    return plan


def _expected_part_size(row: PolarRAGUploadSession, part_number: int) -> int:
    if part_number < row.part_count:
        return cast(int, row.part_size_bytes)
    return cast(
        int,
        row.file_size_bytes - row.part_size_bytes * (row.part_count - 1),
    )


def _verified_oss_parts(
    row: PolarRAGUploadSession,
    parts: Sequence[Any],
    *,
    require_all: bool,
) -> list[Any]:
    by_number: dict[int, Any] = {}
    for part in parts:
        number = getattr(part, "part_number", None)
        etag = getattr(part, "etag", None)
        size = getattr(part, "size", None)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or number > row.part_count
            or number in by_number
            or not isinstance(etag, str)
            or not etag
            or size != _expected_part_size(row, number)
        ):
            raise UploadSessionError("OSS_UPLOAD_INVALID_STATE")
        by_number[number] = part
    if require_all and set(by_number) != set(range(1, row.part_count + 1)):
        raise UploadSessionError("UPLOAD_PARTS_INCOMPLETE")
    return [by_number[number] for number in sorted(by_number)]


def _normalize_etag(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    if (
        not normalized
        or len(normalized) > 256
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        )
    ):
        raise UploadSessionError("INVALID_ARGUMENT")
    return normalized


def _submitted_parts(parts: Sequence[Any], part_count: int) -> dict[int, str]:
    submitted: dict[int, str] = {}
    for part in parts:
        if isinstance(part, dict):
            number = part.get("part_number")
            etag = part.get("etag")
        else:
            number = getattr(part, "part_number", None)
            etag = getattr(part, "etag", None)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or number > part_count
            or number in submitted
            or not isinstance(etag, str)
        ):
            raise UploadSessionError("INVALID_ARGUMENT")
        submitted[number] = _normalize_etag(etag)
    if set(submitted) != set(range(1, part_count + 1)):
        raise UploadSessionError("UPLOAD_PARTS_INCOMPLETE")
    return submitted


async def prepare_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    knowledge_resource_id: str,
    filename: str,
    file_size_bytes: int,
    file_md5: str,
    file_sha256: str,
    content_type: str | None,
    allowed_instance_ids: set[str] | None,
    object_store_factory: ObjectStoreFactory = object_store_from_space,
) -> tuple[KnowledgeResource, dict[str, Any]]:
    if user.role == UserRole.ADMIN or not agent_id:
        raise UploadSessionError("ADMIN_DOCUMENT_UPLOAD_FORBIDDEN")
    (
        normalized_filename,
        file_type,
        normalized_md5,
        normalized_sha256,
        normalized_content_type,
    ) = _validate_metadata(
        filename=filename,
        file_size_bytes=file_size_bytes,
        file_md5=file_md5,
        file_sha256=file_sha256,
        content_type=content_type,
    )
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            [knowledge_resource_id],
            allowed_instance_ids=allowed_instance_ids,
        )
    except KnowledgeAccessError as exc:
        raise UploadSessionError("KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE") from exc
    try:
        document_actor(plan.acl_context)
    except ValueError as exc:
        raise UploadSessionError("IDENTITY_CONTEXT_UNAVAILABLE") from exc
    space = plan.space
    store = _store_for_space(space, object_store_factory)
    key = document_object_key(
        space.oss_object_prefix or "",
        new_upload_object_id(),
        normalized_filename,
    )
    try:
        upload_id = await store.initiate_multipart(key)
    except Exception as exc:
        raise UploadSessionError("OSS_UPLOAD_FAILED") from exc
    now = datetime.now(UTC)
    row = PolarRAGUploadSession(
        pas_user_id=user.id,
        agent_id=agent_id,
        knowledge_resource_id=knowledge_resource_id,
        filename=normalized_filename,
        file_type=file_type,
        content_type=normalized_content_type,
        file_size_bytes=file_size_bytes,
        file_md5=normalized_md5,
        file_sha256=normalized_sha256,
        oss_bucket=space.oss_bucket or "",
        oss_endpoint=space.oss_endpoint or "",
        oss_object_key=key,
        oss_multipart_upload_id=upload_id,
        part_size_bytes=PART_SIZE_BYTES,
        part_count=math.ceil(file_size_bytes / PART_SIZE_BYTES),
        status=PolarRAGUploadStatus.PREPARED,
        expires_at=now + UPLOAD_SESSION_TTL,
    )
    try:
        parts = _part_uploads(
            store,
            row,
            range(1, row.part_count + 1),
        )
    except UploadSessionError:
        try:
            await store.abort_multipart(key, upload_id)
        except Exception:
            pass
        raise
    try:
        session.add(row)
        await session.flush()
    except Exception as exc:
        try:
            await store.abort_multipart(key, upload_id)
        except Exception:
            pass
        raise UploadSessionError("UPLOAD_SESSION_CREATE_FAILED") from exc
    return plan.resources[0], _response(row, parts=parts)


async def resume_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    upload_session_id: str,
    allowed_instance_ids: set[str] | None,
    object_store_factory: ObjectStoreFactory = object_store_from_space,
) -> tuple[KnowledgeResource, dict[str, Any]]:
    row = await _owned_session(
        session,
        user_id=user.id,
        agent_id=agent_id,
        upload_session_id=upload_session_id,
    )
    if row.status == PolarRAGUploadStatus.UPLOADED:
        plan = await _session_access(
            session,
            user,
            row,
            allowed_instance_ids=allowed_instance_ids,
        )
        return plan.resources[0], _response(row, parts=[])
    if row.status != PolarRAGUploadStatus.PREPARED:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACTIVE")
    if _as_utc(row.expires_at) <= datetime.now(UTC):
        raise UploadSessionError("UPLOAD_SESSION_EXPIRED")
    plan = await _session_access(
        session,
        user,
        row,
        allowed_instance_ids=allowed_instance_ids,
    )
    store = _store_for_space(plan.space, object_store_factory, row)
    try:
        listed = await store.list_multipart_parts(
            row.oss_object_key,
            row.oss_multipart_upload_id,
        )
    except Exception as exc:
        raise UploadSessionError("OSS_UPLOAD_FAILED") from exc
    uploaded = _verified_oss_parts(row, listed, require_all=False)
    present = {part.part_number for part in uploaded}
    missing = [
        number
        for number in range(1, row.part_count + 1)
        if number not in present
    ]
    uploaded_payload = [
        {
            "part_number": part.part_number,
            "etag": part.etag,
            "size_bytes": part.size,
        }
        for part in uploaded
    ]
    return plan.resources[0], _response(
        row,
        parts=_part_uploads(store, row, missing),
        uploaded_parts=uploaded_payload,
    )


async def complete_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    upload_session_id: str,
    parts: Sequence[Any],
    allowed_instance_ids: set[str] | None,
    object_store_factory: ObjectStoreFactory = object_store_from_space,
    client_factory: ClientFactory = client_from_instance,
) -> tuple[KnowledgeResource, dict[str, Any]]:
    row = await _owned_session(
        session,
        user_id=user.id,
        agent_id=agent_id,
        upload_session_id=upload_session_id,
    )
    if row.status == PolarRAGUploadStatus.ABORTED:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACTIVE")
    if (
        row.status == PolarRAGUploadStatus.PREPARED
        and _as_utc(row.expires_at) <= datetime.now(UTC)
    ):
        raise UploadSessionError("UPLOAD_SESSION_EXPIRED")
    plan = await _session_access(
        session,
        user,
        row,
        allowed_instance_ids=allowed_instance_ids,
    )
    resource = plan.resources[0]
    if row.status == PolarRAGUploadStatus.COMPLETED:
        return resource, _completed_result(row)
    store = _store_for_space(plan.space, object_store_factory, row)
    if row.status == PolarRAGUploadStatus.PREPARED:
        submitted = _submitted_parts(parts, row.part_count)
        try:
            listed = await store.list_multipart_parts(
                row.oss_object_key,
                row.oss_multipart_upload_id,
            )
        except Exception as exc:
            raise UploadSessionError("OSS_UPLOAD_FAILED") from exc
        verified = _verified_oss_parts(row, listed, require_all=True)
        if any(
            submitted[part.part_number] != _normalize_etag(part.etag)
            for part in verified
        ):
            raise UploadSessionError("UPLOAD_PARTS_INVALID")
        try:
            await store.complete_multipart(
                row.oss_object_key,
                row.oss_multipart_upload_id,
                verified,
            )
        except Exception as exc:
            raise UploadSessionError("OSS_UPLOAD_FAILED") from exc
        row.status = PolarRAGUploadStatus.UPLOADED
        await session.commit()
    try:
        actor = document_actor(plan.acl_context)
    except ValueError as exc:
        raise UploadSessionError("IDENTITY_CONTEXT_UNAVAILABLE") from exc
    acl_context = dict(plan.acl_context)
    acl_context["actor"] = actor
    try:
        document = await client_factory(plan.instance).submit_document(
            plan.space.space_id,
            resource.kb_id,
            oss_path=f"oss://{row.oss_bucket}/{row.oss_object_key}",
            filename=row.filename,
            file_type=row.file_type,
            file_md5=row.file_md5,
            file_size_bytes=row.file_size_bytes,
            metadata={"sha256": row.file_sha256},
            acl_context=acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise UploadSessionError(exc.code.value) from exc
    except Exception as exc:
        raise UploadSessionError("POLARRAG_DOCUMENT_SUBMIT_FAILED") from exc
    row.status = PolarRAGUploadStatus.COMPLETED
    row.completed_at = datetime.now(UTC)
    row.doc_id = document["doc_id"]
    row.upstream_status = document.get("status", "DISPATCHED")
    await session.flush()
    return resource, {
        "doc_id": row.doc_id,
        "status": row.upstream_status,
        "filename": row.filename,
    }


def _completed_result(
    row: PolarRAGUploadSession,
) -> dict[str, Any]:
    if row.doc_id is None:
        raise UploadSessionError("UPLOAD_SESSION_INVALID_STATE")
    return {
        "doc_id": row.doc_id,
        "status": row.upstream_status or "DISPATCHED",
        "filename": row.filename,
    }


async def abort_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    upload_session_id: str,
    object_store_factory: ObjectStoreFactory = object_store_from_space,
) -> tuple[KnowledgeResource, dict[str, Any]]:
    row = await _owned_session(
        session,
        user_id=user.id,
        agent_id=agent_id,
        upload_session_id=upload_session_id,
    )
    resource = await session.get(KnowledgeResource, row.knowledge_resource_id)
    if resource is None:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACCESSIBLE")
    if row.status == PolarRAGUploadStatus.ABORTED:
        return resource, {"status": row.status.value}
    if row.status == PolarRAGUploadStatus.COMPLETED:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACTIVE")
    space = await session.get(PolarRAGSpace, resource.knowledge_space_id)
    if space is None:
        raise UploadSessionError("UPLOAD_SESSION_NOT_ACCESSIBLE")
    store = _store_for_space(space, object_store_factory, row)
    try:
        if row.status == PolarRAGUploadStatus.PREPARED:
            await store.abort_multipart(
                row.oss_object_key,
                row.oss_multipart_upload_id,
            )
        else:
            await store.delete(row.oss_object_key)
    except Exception as exc:
        raise UploadSessionError("OSS_UPLOAD_FAILED") from exc
    row.status = PolarRAGUploadStatus.ABORTED
    await session.flush()
    return resource, {"status": row.status.value}
