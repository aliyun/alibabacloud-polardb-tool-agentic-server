from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import (
    PrincipalAuthenticationError,
    PrincipalKind,
    require_current_actor,
)
from server.core.audit_logger import log_audit
from server.db.engine import get_session_factory
from server.mcp.agent_user_context import (
    allowed_polarrag_instance_ids,
    current_agent_user_context,
)
from server.models import (
    AuditStatus,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeResource,
    PolarRAGInstance,
    User,
)
from server.polarrag.access import (
    KnowledgeAccessError,
    list_visible_knowledge_resources,
    plan_knowledge_access,
)
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import (
    PolarRAGClient,
    PolarRAGErrorCode,
    PolarRAGUpstreamError,
)
from server.polarrag.mcp_upload import (
    UploadSessionError,
    abort_upload,
    complete_upload,
    prepare_upload,
    resume_upload,
)
from server.polarrag.upload import object_store_from_space

POLARRAG_UPLOAD_TOOL_NAMES = frozenset(
    {
        "prepare_document_upload",
        "resume_document_upload",
        "complete_document_upload",
        "abort_document_upload",
    }
)
POLARRAG_TOOL_NAMES = frozenset(
    {
        "list_knowledge_resources",
        "kb_search",
        "kb_fetch_context",
        "doc_find_by_name",
        "doc_status",
        "doc_recall",
        "doc_get_original",
        "doc_delete",
        "doc_rechunk",
    }
) | POLARRAG_UPLOAD_TOOL_NAMES


class UploadPartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=256)

ClientFactory = Callable[[PolarRAGInstance], PolarRAGClient]
ToolHandler = Callable[..., Awaitable[CallToolResult]]
_SENSITIVE_KEY_PARTS = (
    "acl",
    "principal",
    "token",
    "credential",
    "password",
    "secret",
    "index_name",
)
_SAFE_ERROR_MESSAGES = {
    PolarRAGErrorCode.RERANKER_NOT_CONFIGURED.value: (
        "Reranking is not configured for this knowledge Space. "
        "Ask an administrator to configure it or retry without reranking."
    ),
}


def _result(payload: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, separators=(",", ":")),
            )
        ],
        isError=error,
    )


def _error(code: str, message: str | None = None) -> CallToolResult:
    return _result(
        {
            "error": code,
            "message": message or _SAFE_ERROR_MESSAGES.get(code, code),
        },
        error=True,
    )


def _partial_failure_code(
    error: BaseException,
    *,
    include_inaccessible: bool = False,
) -> str | None:
    if not isinstance(error, PolarRAGUpstreamError):
        return None
    if (
        include_inaccessible
        and error.code
        == PolarRAGErrorCode.KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE
    ):
        return cast(str, error.code.value)
    return PolarRAGErrorCode.UNAVAILABLE.value if error.retryable else None


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_payload(item)
            for key, item in value.items()
            if not any(
                part in str(key).lower() for part in _SENSITIVE_KEY_PARTS
            )
        }
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


def _metadata_hints(source: dict[str, Any]) -> dict[str, Any]:
    metadata = source.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return {
        str(key): value
        for key, value in metadata.items()
        if isinstance(value, (str, int, float, bool))
        and not any(
            part in str(key).lower() for part in _SENSITIVE_KEY_PARTS
        )
    }


async def _build_audit_client_info(
    session: AsyncSession,
    user: User,
    resource_ids: list[str],
    result_payload: dict[str, Any],
    *,
    error_code: str | None,
) -> dict[str, Any]:
    resources = list(
        (
            await session.execute(
                select(KnowledgeResource).where(
                    KnowledgeResource.id.in_(resource_ids)
                )
            )
        ).scalars()
    )
    domains = {resource.identity_domain for resource in resources}
    principals: set[tuple[str, EnterprisePrincipalType, str]] = set()
    if domains:
        principals = {
            (provider, principal_type, principal_id)
            for provider, principal_type, principal_id in (
                await session.execute(
                    select(
                        EnterprisePrincipalAssignment.provider,
                        EnterprisePrincipalAssignment.principal_type,
                        EnterprisePrincipalAssignment.principal_id,
                    ).where(
                        EnterprisePrincipalAssignment.pas_user_id == user.id,
                        EnterprisePrincipalAssignment.identity_domain.in_(
                            domains
                        ),
                        EnterprisePrincipalAssignment.status
                        == EnterprisePrincipalStatus.ACTIVE,
                        or_(
                            EnterprisePrincipalAssignment.valid_until.is_(
                                None
                            ),
                            EnterprisePrincipalAssignment.valid_until
                            > datetime.now(UTC),
                        ),
                    )
                )
            ).all()
        }
    hit_count = result_payload.get("returned_count")
    if not isinstance(hit_count, int) or isinstance(hit_count, bool):
        items = result_payload.get("items")
        hit_count = len(items) if isinstance(items, list) else None
    partial_failures = result_payload.get("partial_failures")
    return {
        "knowledge_resource_ids": resource_ids,
        "polarrag_instance_ids": sorted(
            {resource.polarrag_instance_id for resource in resources}
        ),
        "space_ids": sorted(
            {resource.space_id for resource in resources}
        ),
        "kb_ids": sorted({resource.kb_id for resource in resources}),
        "providers": sorted(
            {provider for provider, _principal_type, _id in principals}
        ),
        "principal_count": len(principals),
        "polarrag_status": error_code or "success",
        "hit_count": hit_count,
        "successful_searches": result_payload.get(
            "successful_searches"
        ),
        "failed_searches": result_payload.get("failed_searches"),
        "partial_failure_count": (
            len(partial_failures)
            if isinstance(partial_failures, list)
            else 0
        ),
    }


def _search_hits(
    response: dict[str, Any],
    resource,
) -> list[dict[str, Any]]:
    hits_container = response.get("hits")
    if not isinstance(hits_container, dict):
        raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
    raw_hits = hits_container.get("hits")
    if not isinstance(raw_hits, list):
        raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
    normalized: list[dict[str, Any]] = []
    for raw in raw_hits:
        if not isinstance(raw, dict):
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        source = raw.get("_source")
        if not isinstance(source, dict):
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        if (
            not isinstance(source.get("doc_id"), str)
            or not isinstance(source.get("chunk_index"), int)
            or isinstance(source.get("chunk_index"), bool)
        ):
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        score = raw.get("_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            score = 0.0
        normalized.append(
            {
                "score": float(score),
                "text": source.get("text"),
                "knowledge_resource_id": resource.id,
                "knowledge_resource_name": resource.name,
                "knowledge_space_id": resource.knowledge_space_id,
                "knowledge_space_name": resource.space.name,
                "doc_id": source.get("doc_id"),
                "filename": source.get("filename"),
                "chunk_index": source.get("chunk_index"),
                "page_numbers": source.get("page_numbers") or [],
                "headings": source.get("headings") or [],
                "captions": source.get("captions") or [],
                "doc_items": source.get("doc_items") or [],
                "metadata_hints": _metadata_hints(source),
            }
        )
    return normalized


async def handle_list_knowledge_resources(
    session: AsyncSession,
    user: User,
    *,
    cursor: str | None = None,
    limit: int = 50,
    allowed_instance_ids: set[str] | None = None,
) -> CallToolResult:
    if limit < 1 or limit > 200:
        return _error("INVALID_ARGUMENT")
    resources = await list_visible_knowledge_resources(
        session,
        user,
        allowed_instance_ids=allowed_instance_ids,
    )
    if cursor is not None:
        positions = [
            index
            for index, resource in enumerate(resources)
            if resource.id == cursor
        ]
        if not positions:
            return _error("INVALID_CURSOR")
        resources = resources[positions[0] + 1 :]
    page = resources[:limit]
    next_cursor = (
        page[-1].id if len(resources) > len(page) and page else None
    )
    return _result(
        {
            "items": [
                {
                    "knowledge_resource_id": resource.id,
                    "knowledge_space_id": resource.knowledge_space_id,
                    "knowledge_space_name": resource.space.name,
                    "name": resource.name,
                    "kb_type": resource.kb_type,
                    "usage": resource.usage,
                }
                for resource in page
            ],
            "next_cursor": next_cursor,
        }
    )


async def handle_kb_search(
    session: AsyncSession,
    user: User,
    *,
    query: str,
    knowledge_resource_ids: list[str],
    search_mode: str = "balanced",
    top_k: int = 10,
    min_score: float | None = None,
    reranker: bool = False,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    normalized_mode = (
        "balanced" if search_mode.lower() == "auto" else search_mode.lower()
    )
    if (
        not query.strip()
        or top_k < 1
        or top_k > 1000
        or min_score is not None
        and (not math.isfinite(min_score) or min_score < 0)
        or normalized_mode
        not in {"balanced", "precise", "semantic", "rrf", "knn"}
    ):
        return _error("INVALID_ARGUMENT")
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            knowledge_resource_ids,
            allowed_instance_ids=allowed_instance_ids,
        )
    except KnowledgeAccessError as exc:
        return _error(exc.code.value)
    client = client_factory(plan.instance)
    calls = [
        client.search(
            plan.space.space_id,
            resource.kb_id,
            query=query.strip(),
            search_mode=normalized_mode,
            top_k=top_k,
            min_score=min_score,
            reranker=reranker,
            acl_context=plan.acl_context,
        )
        for resource in plan.resources
    ]
    responses = await asyncio.gather(*calls, return_exceptions=True)
    results: list[dict[str, Any]] = []
    partial_failures = list(plan.partial_failures)
    successful = 0
    for resource, response in zip(plan.resources, responses):
        if isinstance(response, BaseException):
            partial_code = _partial_failure_code(
                response,
                include_inaccessible=True,
            )
            if partial_code is not None:
                partial_failures.append(
                    {
                        "knowledge_resource_id": resource.id,
                        "error": partial_code,
                    }
                )
                continue
            return _error(
                response.code.value
                if isinstance(response, PolarRAGUpstreamError)
                else PolarRAGErrorCode.UNAVAILABLE.value
            )
        try:
            hits = _search_hits(
                cast(dict[str, Any], response),
                resource,
            )
        except PolarRAGUpstreamError as exc:
            return _error(exc.code.value)
        successful += 1
        results.extend(hits)
    if successful == 0:
        return _error(PolarRAGErrorCode.UNAVAILABLE.value)
    results.sort(key=lambda item: item["score"], reverse=True)
    results = results[:top_k]
    return _result(
        {
            "results": results,
            "total_hits": len(results),
            "returned_count": len(results),
            "max_score": results[0]["score"] if results else None,
            "successful_searches": successful,
            "failed_searches": len(plan.resources) - successful,
            "partial_failures": partial_failures,
        }
    )


async def _single_resource_call(
    session: AsyncSession,
    user: User,
    knowledge_resource_id: str,
    operation: Callable[
        [PolarRAGClient, Any, dict[str, Any]], Any
    ],
    *,
    client_factory: ClientFactory,
    allowed_instance_ids: set[str] | None = None,
) -> CallToolResult:
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            [knowledge_resource_id],
            allowed_instance_ids=allowed_instance_ids,
        )
    except KnowledgeAccessError as exc:
        return _error(exc.code.value)
    resource = plan.resources[0]
    try:
        payload = await operation(
            client_factory(plan.instance),
            resource,
            plan.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        return _error(exc.code.value)
    return _result(
        {
            "knowledge_resource_id": resource.id,
            "knowledge_space_id": resource.knowledge_space_id,
            "result": _sanitize_payload(payload),
        }
    )


async def _authorized_document_info(
    client: PolarRAGClient,
    resource: KnowledgeResource,
    doc_id: str,
    acl_context: dict[str, Any],
) -> dict[str, Any]:
    payload = await client.document_info(
        resource.space_id,
        doc_id,
        acl_context=acl_context,
    )
    if (
        payload.get("doc_id") != doc_id
        or payload.get("space_id") != resource.space_id
        or payload.get("kb_id") != resource.kb_id
    ):
        raise PolarRAGUpstreamError(
            PolarRAGErrorCode.DOCUMENT_NOT_ACCESSIBLE
        )
    return payload


async def handle_kb_fetch_context(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    chunk_index: int,
    window_size: int = 2,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id or chunk_index < 0 or window_size < 0 or window_size > 100:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        await _authorized_document_info(
            client,
            resource,
            doc_id,
            acl_context,
        )
        return await client.fetch_context(
            resource.space_id,
            doc_id,
            chunk_index=chunk_index,
            window_size=window_size,
            acl_context=acl_context,
        )

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        client_factory=client_factory,
        allowed_instance_ids=allowed_instance_ids,
    )


async def handle_doc_find_by_name(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_ids: list[str],
    filename: str,
    limit: int = 20,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not filename.strip() or limit < 1 or limit > 1000:
        return _error("INVALID_ARGUMENT")
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            knowledge_resource_ids,
            allowed_instance_ids=allowed_instance_ids,
        )
    except KnowledgeAccessError as exc:
        return _error(exc.code.value)
    client = client_factory(plan.instance)
    responses = await asyncio.gather(
        *[
            client.find_by_name(
                resource.space_id,
                kb_id=resource.kb_id,
                filename=filename.strip(),
                limit=limit,
                acl_context=plan.acl_context,
            )
            for resource in plan.resources
        ],
        return_exceptions=True,
    )
    items: list[dict[str, Any]] = []
    partial_failures = list(plan.partial_failures)
    successful = 0
    for resource, response in zip(plan.resources, responses):
        if isinstance(response, BaseException):
            partial_code = _partial_failure_code(response)
            if partial_code is not None:
                partial_failures.append(
                    {
                        "knowledge_resource_id": resource.id,
                        "error": partial_code,
                    }
                )
                continue
            code = (
                response.code.value
                if isinstance(response, PolarRAGUpstreamError)
                else PolarRAGErrorCode.UNAVAILABLE.value
            )
            return _error(code)
        successful += 1
        sanitized = _sanitize_payload(response)
        matches = (
            sanitized.get("items")
            if isinstance(sanitized, dict)
            else None
        )
        if not isinstance(matches, list):
            matches = [sanitized]
        items.extend(
            {
                "knowledge_resource_id": resource.id,
                "knowledge_space_id": resource.knowledge_space_id,
                "document": item,
            }
            for item in matches
        )
    if successful == 0:
        return _error(PolarRAGErrorCode.UNAVAILABLE.value)
    return _result(
        {"items": items, "partial_failures": partial_failures}
    )


async def handle_doc_status(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        payload = await _authorized_document_info(
            client,
            resource,
            doc_id,
            acl_context,
        )
        allowed = {
            "doc_id",
            "kb_id",
            "filename",
            "status",
            "chunk_count",
            "created_at",
            "updated_at",
            "completed_at",
            "active_generation",
            "revision_status",
        }
        return {key: value for key, value in payload.items() if key in allowed}

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        client_factory=client_factory,
        allowed_instance_ids=allowed_instance_ids,
    )


async def handle_doc_recall(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    query: str,
    top_k: int = 10,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id or not query.strip() or top_k < 1 or top_k > 1000:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        return await client.recall_document(
            resource.space_id,
            doc_id,
            kb_id=resource.kb_id,
            query=query.strip(),
            top_k=top_k,
            acl_context=acl_context,
        )

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        client_factory=client_factory,
        allowed_instance_ids=allowed_instance_ids,
    )


async def handle_doc_get_original(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        payload = await _authorized_document_info(
            client,
            resource,
            doc_id,
            acl_context,
        )
        result = {
            key: payload[key]
            for key in ("doc_id", "filename", "file_type", "oss_path")
            if key in payload
        }
        size = payload.get(
            "size",
            payload.get("file_size", payload.get("file_size_bytes")),
        )
        if size is not None:
            result["size"] = size
        content_hash = payload.get("content_hash", payload.get("file_md5"))
        if content_hash is not None:
            result["content_hash"] = content_hash
        return result

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        client_factory=client_factory,
        allowed_instance_ids=allowed_instance_ids,
    )


async def handle_doc_delete(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        await _authorized_document_info(
            client,
            resource,
            doc_id,
            acl_context,
        )
        payload = await client.delete_document(
            resource.space_id,
            doc_id,
            acl_context=acl_context,
        )
        if payload.get("doc_id") != doc_id:
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        return {
            key: payload[key]
            for key in ("doc_id", "task_id", "status")
            if key in payload
        }

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        client_factory=client_factory,
        allowed_instance_ids=allowed_instance_ids,
    )


async def handle_doc_rechunk(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    chunk_strategy: str = "inherit",
    chunk_max_tokens: int | None = None,
    allowed_instance_ids: set[str] | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    strategy = chunk_strategy.lower()
    if (
        not doc_id
        or strategy not in {"inherit", "hybrid", "hierarchical"}
        or chunk_max_tokens is not None
        and (chunk_max_tokens < 1 or chunk_max_tokens > 100_000)
        or strategy == "inherit"
        and chunk_max_tokens is not None
    ):
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        await _authorized_document_info(
            client,
            resource,
            doc_id,
            acl_context,
        )
        payload = await client.rechunk_document(
            resource.space_id,
            doc_id,
            chunk_strategy=None if strategy == "inherit" else strategy,
            chunk_max_tokens=chunk_max_tokens,
            acl_context=acl_context,
        )
        if payload.get("doc_id") != doc_id:
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        return {
            key: payload[key]
            for key in (
                "doc_id",
                "status",
                "noop",
                "previous_generation",
                "target_generation",
                "task_id",
            )
            if key in payload
        }

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        client_factory=client_factory,
        allowed_instance_ids=allowed_instance_ids,
    )


def _upload_tool_result(
    resource: KnowledgeResource,
    payload: dict[str, Any],
) -> CallToolResult:
    return _result(
        {
            "knowledge_resource_id": resource.id,
            "knowledge_space_id": resource.knowledge_space_id,
            "result": payload,
        }
    )


async def handle_prepare_document_upload(
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
    allowed_instance_ids: set[str] | None = None,
    object_store_factory=object_store_from_space,
) -> CallToolResult:
    try:
        resource, payload = await prepare_upload(
            session,
            user,
            agent_id=agent_id,
            knowledge_resource_id=knowledge_resource_id,
            filename=filename,
            file_size_bytes=file_size_bytes,
            file_md5=file_md5,
            file_sha256=file_sha256,
            content_type=content_type,
            allowed_instance_ids=allowed_instance_ids,
            object_store_factory=object_store_factory,
        )
    except UploadSessionError as exc:
        return _error(exc.code)
    return _upload_tool_result(resource, payload)


async def handle_resume_document_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    upload_session_id: str,
    allowed_instance_ids: set[str] | None = None,
    object_store_factory=object_store_from_space,
) -> CallToolResult:
    try:
        resource, payload = await resume_upload(
            session,
            user,
            agent_id=agent_id,
            upload_session_id=upload_session_id,
            allowed_instance_ids=allowed_instance_ids,
            object_store_factory=object_store_factory,
        )
    except UploadSessionError as exc:
        return _error(exc.code)
    return _upload_tool_result(resource, payload)


async def handle_complete_document_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    upload_session_id: str,
    parts: Sequence[Any],
    allowed_instance_ids: set[str] | None = None,
    object_store_factory=object_store_from_space,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    try:
        resource, payload = await complete_upload(
            session,
            user,
            agent_id=agent_id,
            upload_session_id=upload_session_id,
            parts=parts,
            allowed_instance_ids=allowed_instance_ids,
            object_store_factory=object_store_factory,
            client_factory=client_factory,
        )
    except UploadSessionError as exc:
        return _error(exc.code)
    return _upload_tool_result(resource, payload)


async def handle_abort_document_upload(
    session: AsyncSession,
    user: User,
    *,
    agent_id: str,
    upload_session_id: str,
    allowed_instance_ids: set[str] | None = None,
    object_store_factory=object_store_from_space,
) -> CallToolResult:
    del allowed_instance_ids
    try:
        resource, payload = await abort_upload(
            session,
            user,
            agent_id=agent_id,
            upload_session_id=upload_session_id,
            object_store_factory=object_store_factory,
        )
    except UploadSessionError as exc:
        return _error(exc.code)
    return _upload_tool_result(resource, payload)


async def _current_user(session: AsyncSession) -> User | None:
    token = get_access_token()
    if token is None or not token.subject:
        return None
    try:
        actor = await require_current_actor(
            session,
            token.subject,
            PrincipalKind.USER,
        )
    except PrincipalAuthenticationError:
        return None
    return actor if isinstance(actor, User) else None


async def _execute_tool(
    tool_name: str,
    handler: ToolHandler,
    *,
    require_agent_user_token: bool = False,
    **kwargs: Any,
) -> CallToolResult:
    started_at = time.perf_counter()
    async with get_session_factory()() as session:
        agent_context = None
        if require_agent_user_token:
            agent_context = await current_agent_user_context(session)
            if agent_context is None:
                return _error("USER_AGENT_TOKEN_REQUIRED")
            user = agent_context.user
        else:
            current_user = await _current_user(session)
            if current_user is None:
                return _error("AUTH_REQUIRED")
            user = current_user
        instance_ids = await allowed_polarrag_instance_ids(
            session,
            context=agent_context,
        )
        if agent_context is not None:
            kwargs["agent_id"] = agent_context.agent.id
        result = await handler(
            session,
            user,
            allowed_instance_ids=instance_ids,
            **kwargs,
        )
        error_code = None
        result_payload: dict[str, Any] = {}
        if result.content:
            try:
                decoded = json.loads(
                    cast(TextContent, result.content[0]).text
                )
                if isinstance(decoded, dict):
                    result_payload = decoded
                candidate = result_payload.get("error")
                if isinstance(candidate, str):
                    error_code = candidate
            except (AttributeError, TypeError, ValueError):
                if result.isError:
                    error_code = "POLARRAG_TOOL_FAILED"
        resource_ids = kwargs.get("knowledge_resource_ids")
        if resource_ids is None:
            single_resource = kwargs.get("knowledge_resource_id")
            resource_ids = (
                [single_resource] if single_resource is not None else []
            )
        if not resource_ids:
            result_resource_id = result_payload.get("knowledge_resource_id")
            if isinstance(result_resource_id, str):
                resource_ids = [result_resource_id]
        if not resource_ids:
            items = result_payload.get("items")
            if isinstance(items, list):
                resource_ids = [
                    item["knowledge_resource_id"]
                    for item in items
                    if isinstance(item, dict)
                    and isinstance(
                        item.get("knowledge_resource_id"), str
                    )
                ]
        normalized_resource_ids = (
            resource_ids if isinstance(resource_ids, list) else []
        )
        audit_info = await _build_audit_client_info(
            session,
            user,
            normalized_resource_ids,
            result_payload,
            error_code=error_code,
        )
        await log_audit(
            session,
            user_id=user.id,
            action=f"polarrag.{tool_name}",
            target_type="knowledge_resource",
            target_id=(
                normalized_resource_ids[0]
                if len(normalized_resource_ids) == 1
                else None
            ),
            status=(
                AuditStatus.ERROR
                if result.isError
                else AuditStatus.SUCCESS
            ),
            error_code=error_code,
            duration_ms=max(
                0,
                int((time.perf_counter() - started_at) * 1000),
            ),
            client_info=json.dumps(audit_info, separators=(",", ":")),
            required=True,
        )
        return result


def register_polarrag_tools(mcp) -> None:
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    mutation_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    delete_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
    create_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )

    @mcp.tool(
        description=(
            "Discover the PolarRAG knowledge resources available to the "
            "authenticated PAS user through this Agent. Call this first, "
            "keep the opaque knowledge_resource_id values for later tools, "
            "and group resources by knowledge_space_id because a multi-resource "
            "operation cannot cross Spaces. Continue with next_cursor until it "
            "is null."
        ),
        annotations=annotations,
    )
    async def list_knowledge_resources(
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> CallToolResult:
        return await _execute_tool(
            "list_knowledge_resources",
            handle_list_knowledge_resources,
            cursor=cursor,
            limit=limit,
        )

    @mcp.tool(
        description=(
            "Search across one or more accessible PolarRAG knowledge bases. "
            "Pass only opaque knowledge_resource_ids returned by "
            "list_knowledge_resources; every ID in one call must belong to the "
            "same PolarRAG instance and Space. Results are merged and ranked "
            "across KBs. Always inspect partial_failures because an unavailable "
            "or inaccessible KB does not discard successful KB results. Use "
            "doc_recall instead when the doc_id is already known. If reranker "
            "is true but the Space has no reranker configuration, the tool "
            "returns RERANKER_NOT_CONFIGURED without exposing upstream details."
        ),
        annotations=annotations,
    )
    async def kb_search(
        query: Annotated[str, Field(min_length=1, max_length=10000)],
        knowledge_resource_ids: Annotated[
            list[Annotated[str, Field(min_length=36, max_length=36)]],
            Field(min_length=1, max_length=50),
        ],
        search_mode: str = "balanced",
        top_k: Annotated[int, Field(ge=1, le=1000)] = 10,
        min_score: Annotated[float | None, Field(ge=0)] = None,
        reranker: bool = False,
    ) -> CallToolResult:
        return await _execute_tool(
            "kb_search",
            handle_kb_search,
            query=query,
            knowledge_resource_ids=knowledge_resource_ids,
            search_mode=search_mode,
            top_k=top_k,
            min_score=min_score,
            reranker=reranker,
        )

    @mcp.tool(
        description=(
            "Expand a PolarRAG search hit with ACL-authorized neighboring "
            "chunks. Pass the hit's knowledge_resource_id, doc_id, and "
            "chunk_index; window_size controls how many chunks on each side "
            "are returned. Call this after kb_search or doc_recall when the hit "
            "needs more surrounding text."
        ),
        annotations=annotations,
    )
    async def kb_fetch_context(
        knowledge_resource_id: Annotated[str, Field(min_length=36, max_length=36)],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
        chunk_index: Annotated[int, Field(ge=0)],
        window_size: Annotated[int, Field(ge=0, le=100)] = 2,
    ) -> CallToolResult:
        return await _execute_tool(
            "kb_fetch_context",
            handle_kb_fetch_context,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
            chunk_index=chunk_index,
            window_size=window_size,
        )

    @mcp.tool(
        description=(
            "Find ACL-authorized documents by filename across one or more "
            "knowledge resources in the same PolarRAG instance and Space. Pass "
            "opaque knowledge_resource_ids from list_knowledge_resources. Use "
            "this when the filename is known but doc_id is not, inspect "
            "partial_failures, then pass a returned doc_id to document tools."
        ),
        annotations=annotations,
    )
    async def doc_find_by_name(
        knowledge_resource_ids: Annotated[
            list[Annotated[str, Field(min_length=36, max_length=36)]],
            Field(min_length=1, max_length=50),
        ],
        filename: Annotated[str, Field(min_length=1, max_length=1024)],
        limit: Annotated[int, Field(ge=1, le=1000)] = 20,
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_find_by_name",
            handle_doc_find_by_name,
            knowledge_resource_ids=knowledge_resource_ids,
            filename=filename,
            limit=limit,
        )

    @mcp.tool(
        description=(
            "Read the processing and indexing status of one ACL-authorized "
            "document. Pass its knowledge_resource_id and globally unique "
            "doc_id from kb_search or doc_find_by_name. The result may include "
            "status, chunk count, generation state, and timestamps; it does not "
            "return document content."
        ),
        annotations=annotations,
    )
    async def doc_status(
        knowledge_resource_id: Annotated[str, Field(min_length=36, max_length=36)],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_status",
            handle_doc_status,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
        )

    @mcp.tool(
        description=(
            "Search for relevant ACL-authorized chunks within one document. "
            "Use this for focused questions after kb_search or "
            "doc_find_by_name has provided the knowledge_resource_id and "
            "doc_id. It does not search other documents or knowledge bases; use "
            "kb_search for cross-document retrieval."
        ),
        annotations=annotations,
    )
    async def doc_recall(
        knowledge_resource_id: Annotated[str, Field(min_length=36, max_length=36)],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
        query: Annotated[str, Field(min_length=1, max_length=10000)],
        top_k: Annotated[int, Field(ge=1, le=1000)] = 10,
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_recall",
            handle_doc_recall,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
            query=query,
            top_k=top_k,
        )

    @mcp.tool(
        description=(
            "Return original-file metadata only after PolarRAG authorizes READ "
            "access to the document. The result may contain filename, file "
            "type, size, content hash, and oss_path. PAS does not download or "
            "proxy the file and never returns OSS credentials. Use this only "
            "when the user asks for the original file or its location."
        ),
        annotations=annotations,
    )
    async def doc_get_original(
        knowledge_resource_id: Annotated[str, Field(min_length=36, max_length=36)],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_get_original",
            handle_doc_get_original,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
        )

    @mcp.tool(
        description=(
            "Delete one PolarRAG document after PolarRAG authorizes MANAGE for "
            "the authenticated PAS user. Pass the globally unique doc_id and "
            "its opaque knowledge_resource_id from doc_find_by_name. This is "
            "destructive and must be called only after the user explicitly "
            "requests deletion; PAS rechecks that the document belongs to the "
            "selected KB and never accepts caller-supplied ACL identity."
        ),
        annotations=delete_annotations,
    )
    async def doc_delete(
        knowledge_resource_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_delete",
            handle_doc_delete,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
        )

    @mcp.tool(
        description=(
            "Start an asynchronous rechunk of one PolarRAG document after "
            "PolarRAG authorizes EXECUTE for the authenticated PAS user. Pass "
            "the globally unique doc_id and its opaque knowledge_resource_id. "
            "Choose hybrid or hierarchical with an optional positive "
            "chunk_max_tokens; choose inherit to restore the current Space "
            "strategy (which may return noop when nothing changes). PAS "
            "rechecks that the document belongs to the selected KB."
        ),
        annotations=mutation_annotations,
    )
    async def doc_rechunk(
        knowledge_resource_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
        chunk_strategy: Literal[
            "inherit", "hybrid", "hierarchical"
        ] = "inherit",
        chunk_max_tokens: Annotated[
            int | None, Field(ge=1, le=100_000)
        ] = None,
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_rechunk",
            handle_doc_rechunk,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
            chunk_strategy=chunk_strategy,
            chunk_max_tokens=chunk_max_tokens,
        )

    @mcp.tool(
        description=(
            "Prepare a resumable direct-to-OSS upload for one accessible "
            "PolarRAG knowledge resource. Pass only local file metadata; do "
            "not send a local path or file bytes. Give the returned short-lived "
            "part URLs to the approved local upload script, then call "
            "complete_document_upload with its part results. PAS derives the "
            "OSS destination and enterprise identity on the server."
        ),
        annotations=create_annotations,
    )
    async def prepare_document_upload(
        knowledge_resource_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
        filename: Annotated[str, Field(min_length=1, max_length=512)],
        file_size_bytes: Annotated[int, Field(ge=1, le=100 * 1024 * 1024)],
        file_md5: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{32}$")],
        file_sha256: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")],
        content_type: Annotated[
            str | None, Field(min_length=1, max_length=255)
        ] = None,
    ) -> CallToolResult:
        return await _execute_tool(
            "prepare_document_upload",
            handle_prepare_document_upload,
            require_agent_user_token=True,
            knowledge_resource_id=knowledge_resource_id,
            filename=filename,
            file_size_bytes=file_size_bytes,
            file_md5=file_md5,
            file_sha256=file_sha256,
            content_type=content_type,
        )

    @mcp.tool(
        description=(
            "Resume an unfinished direct-to-OSS upload owned by the current "
            "PAS user and Agent. Returns the already uploaded part metadata "
            "and fresh short-lived URLs only for missing parts."
        ),
        annotations=annotations,
    )
    async def resume_document_upload(
        upload_session_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
    ) -> CallToolResult:
        return await _execute_tool(
            "resume_document_upload",
            handle_resume_document_upload,
            require_agent_user_token=True,
            upload_session_id=upload_session_id,
        )

    @mcp.tool(
        description=(
            "Finalize a prepared direct-to-OSS multipart upload after the "
            "local upload script reports every part. PAS verifies the parts, "
            "rebuilds trusted identity, submits the OSS object to PolarRAG, "
            "and returns PolarRAG's authoritative doc_id and processing status."
        ),
        annotations=mutation_annotations,
    )
    async def complete_document_upload(
        upload_session_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
        parts: Annotated[
            list[UploadPartInput], Field(min_length=1, max_length=1000)
        ],
    ) -> CallToolResult:
        return await _execute_tool(
            "complete_document_upload",
            handle_complete_document_upload,
            require_agent_user_token=True,
            upload_session_id=upload_session_id,
            parts=parts,
        )

    @mcp.tool(
        description=(
            "Abort an unfinished direct-to-OSS upload owned by the current "
            "PAS user and Agent. This removes its temporary OSS upload data "
            "and cannot abort a document already submitted to PolarRAG."
        ),
        annotations=delete_annotations,
    )
    async def abort_document_upload(
        upload_session_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
    ) -> CallToolResult:
        return await _execute_tool(
            "abort_document_upload",
            handle_abort_document_upload,
            require_agent_user_token=True,
            upload_session_id=upload_session_id,
        )
