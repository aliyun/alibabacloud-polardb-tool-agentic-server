from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import (
    PrincipalAuthenticationError,
    PrincipalKind,
    require_current_actor,
)
from server.auth.token_claims import access_token_agent_id
from server.auth.personal_access import is_personal_access
from server.core.audit_logger import log_audit
from server.core.polarrag_governance import (
    PolarRAGGovernanceError,
    PolarRAGToolGovernor,
    get_polarrag_tool_governor,
)
from server.db.engine import get_session_factory
from server.mcp.agent_user_context import (
    current_agent_user_context,
    current_polarrag_resource_scope,
    resolve_polarrag_resource_scope_for_agent,
)
from server.mcp.workspace_context import (
    MCPWorkspaceUnavailable,
    resolve_mcp_workspace_context,
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
    KnowledgeResourceScope,
    list_visible_knowledge_resources_page,
    plan_exhaustive_knowledge_access,
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
    complete_upload,
    prepare_upload,
)
from server.polarrag.upload import object_store_from_space
from server.polarrag.write_policy import require_pas_managed_resource

from server.mcp.knowledge_tools import POLARRAG_TOOL_NAMES, POLARRAG_UPLOAD_TOOL_NAMES  # noqa: F401


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
    PolarRAGErrorCode.CREDENTIAL_UNAVAILABLE.value: (
        "PolarRAG credentials are unavailable. Ask an administrator to verify the instance configuration."
    ),
    PolarRAGErrorCode.RERANKER_NOT_CONFIGURED.value: (
        "Reranking is not configured for this knowledge Space. "
        "Ask an administrator to configure it or retry without reranking."
    ),
    PolarRAGErrorCode.DOCUMENT_UPLOAD_FORBIDDEN.value: (
        "PolarRAG denied document upload for this enterprise identity. "
        "Ask an administrator to verify the Space identity domain and "
        "canonical PAS user ownership."
    ),
    PolarRAGErrorCode.EXTERNAL_SYNC_RESOURCE_READ_ONLY.value: (
        "Externally synchronized knowledge resources are read-only in PAS."
    ),
}
_governance_audit_error: ContextVar[PolarRAGGovernanceError | None] = ContextVar(
    "polarrag_governance_audit_error", default=None
)


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


def _governance_error(error: PolarRAGGovernanceError) -> CallToolResult:
    _governance_audit_error.set(error)
    return _result(error.public_payload(), error=True)


def _partial_failure_code(
    error: BaseException,
    *,
    include_inaccessible: bool = False,
) -> str | None:
    if not isinstance(error, PolarRAGUpstreamError):
        return None
    if include_inaccessible and error.code == PolarRAGErrorCode.KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE:
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


def _image_resources(source: dict[str, Any]) -> list[dict[str, Any]]:
    resources = source.get("image_resources")
    if not isinstance(resources, list):
        return []
    return [dict(resource) for resource in resources if isinstance(resource, dict)]


def _with_image_resources(payload: dict[str, Any]) -> dict[str, Any]:
    hits = payload.get("hits")
    if not isinstance(hits, dict):
        return payload
    raw_hits = hits.get("hits")
    if not isinstance(raw_hits, list):
        return payload
    normalized_hits = []
    for raw_hit in raw_hits:
        if not isinstance(raw_hit, dict):
            normalized_hits.append(raw_hit)
            continue
        source = raw_hit.get("_source")
        if not isinstance(source, dict):
            normalized_hits.append(raw_hit)
            continue
        normalized_source = dict(source)
        normalized_source["image_resources"] = _image_resources(source)
        normalized_hit = dict(raw_hit)
        normalized_hit["_source"] = normalized_source
        normalized_hits.append(normalized_hit)
    normalized_payload = dict(payload)
    normalized_payload["hits"] = {**hits, "hits": normalized_hits}
    return normalized_payload


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
                "image_resources": _image_resources(source),
                "metadata_hints": _metadata_hints(source),
            }
        )
    return normalized


def _supports_multi_kb_search(version: str | None) -> bool:
    if version is None:
        return False
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", version.strip())
    return bool(match and tuple(int(value) for value in match.groups()) >= (1, 0, 7))


def _search_hits_many(
    response: dict[str, Any],
    resources_by_kb_id: dict[str, Any],
) -> list[dict[str, Any]]:
    hits_container = response.get("hits")
    raw_hits = hits_container.get("hits") if isinstance(hits_container, dict) else None
    if not isinstance(raw_hits, list):
        raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
    normalized: list[dict[str, Any]] = []
    for raw in raw_hits:
        source = raw.get("_source") if isinstance(raw, dict) else None
        kb_id = source.get("kb_id") if isinstance(source, dict) else None
        if not isinstance(kb_id, str):
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        resource = resources_by_kb_id.get(kb_id)
        if resource is None:
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        single_response = {"hits": {"hits": [raw]}}
        normalized.extend(_search_hits(single_response, resource))
    return normalized


async def _run_search_calls_in_waves(
    governor: PolarRAGToolGovernor,
    instance_id: str,
    calls: list[
        tuple[list[Any], Callable[[], Awaitable[dict[str, Any]]]]
    ],
) -> list[tuple[list[Any], dict[str, Any] | BaseException]]:
    wave_limit = await governor.upstream_wave_limit()
    completed: list[tuple[list[Any], dict[str, Any] | BaseException]] = []
    for offset in range(0, len(calls), wave_limit):
        wave = calls[offset : offset + wave_limit]
        async with governor.reserve_instance(
            "kb_search",
            instance_id,
            len(wave),
        ):
            responses = await asyncio.gather(
                *(call() for _resources, call in wave),
                return_exceptions=True,
            )
        completed.extend(
            (resources, cast(dict[str, Any] | BaseException, response))
            for (resources, _call), response in zip(wave, responses)
        )
    return completed


async def handle_list_knowledge_resources(
    session: AsyncSession,
    user: User,
    *,
    cursor: str | None = None,
    limit: int = 50,
    resource_scope: KnowledgeResourceScope | None = None,
) -> CallToolResult:
    if limit < 1 or limit > 200:
        return _error("INVALID_ARGUMENT")
    try:
        offset = 0 if cursor is None else int(cursor)
    except ValueError:
        return _error("INVALID_CURSOR")
    if offset < 0:
        return _error("INVALID_CURSOR")
    page, total = await list_visible_knowledge_resources_page(
        session,
        user,
        resource_scope=resource_scope,
        offset=offset,
        limit=limit,
    )
    next_cursor = str(offset + len(page)) if offset + len(page) < total else None
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
    knowledge_resource_ids: list[str] | None = None,
    search_mode: str = "balanced",
    top_k: int = 10,
    min_score: float | None = None,
    reranker: bool = False,
    resource_scope: KnowledgeResourceScope | None = None,
    client_factory: ClientFactory = client_from_instance,
    governor: PolarRAGToolGovernor | None = None,
) -> CallToolResult:
    normalized_mode = "balanced" if search_mode.lower() == "auto" else search_mode.lower()
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
        exhaustive_plan = await plan_exhaustive_knowledge_access(
            session,
            user,
            knowledge_resource_ids,
            resource_scope=resource_scope,
        )
    except KnowledgeAccessError as exc:
        return _error(exc.code.value)
    governor = governor or get_polarrag_tool_governor()
    results: list[dict[str, Any]] = []
    partial_failures = list(exhaustive_plan.partial_failures)
    successful = 0
    try:
        for plan in exhaustive_plan.plans:
            client = client_factory(plan.instance)
            multi_kb = _supports_multi_kb_search(plan.instance.plugin_version)
            calls: list[
                tuple[
                    list[Any],
                    Callable[[], Awaitable[dict[str, Any]]],
                ]
            ]
            if multi_kb:
                try:
                    async with governor.reserve_instance(
                        "kb_search",
                        plan.instance.id,
                        1,
                    ):
                        capabilities = await client.get_search_capabilities()
                except PolarRAGUpstreamError as exc:
                    if exc.status_code not in {400, 404, 501}:
                        raise
                    multi_kb = False
            if multi_kb:
                batch_size = capabilities.max_kb_ids
                batches = [
                    plan.resources[offset : offset + batch_size]
                    for offset in range(0, len(plan.resources), batch_size)
                ]
                calls = [
                    (
                        batch,
                        cast(
                            Callable[[], Awaitable[dict[str, Any]]],
                            lambda batch=batch: client.search_many(
                                plan.space.space_id,
                                [resource.kb_id for resource in batch],
                                query=query.strip(),
                                search_mode=normalized_mode,
                                top_k=top_k,
                                min_score=min_score,
                                reranker=reranker,
                                acl_context=plan.acl_context,
                            ),
                        ),
                    )
                    for batch in batches
                ]
            else:
                calls = [
                    (
                        [resource],
                        cast(
                            Callable[[], Awaitable[dict[str, Any]]],
                            lambda resource=resource: client.search(
                                plan.space.space_id,
                                resource.kb_id,
                                query=query.strip(),
                                search_mode=normalized_mode,
                                top_k=top_k,
                                min_score=min_score,
                                reranker=reranker,
                                acl_context=plan.acl_context,
                            ),
                        ),
                    )
                    for resource in plan.resources
                ]
            completed = await _run_search_calls_in_waves(
                governor,
                plan.instance.id,
                calls,
            )
            for resources, response in completed:
                if isinstance(response, BaseException):
                    partial_code = _partial_failure_code(
                        response,
                        include_inaccessible=True,
                    )
                    if partial_code is not None:
                        partial_failures.extend(
                            {
                                "knowledge_resource_id": resource.id,
                                "error": partial_code,
                            }
                            for resource in resources
                        )
                        continue
                    return _error(
                        response.code.value
                        if isinstance(response, PolarRAGUpstreamError)
                        else PolarRAGErrorCode.UNAVAILABLE.value
                    )
                hits = (
                    _search_hits_many(
                        response,
                        {resource.kb_id: resource for resource in resources},
                    )
                    if multi_kb
                    else _search_hits(response, resources[0])
                )
                successful += len(resources)
                results.extend(hits)
    except PolarRAGGovernanceError as exc:
        return _governance_error(exc)
    except PolarRAGUpstreamError as exc:
        return _error(exc.code.value)
    if successful == 0:
        return _error(PolarRAGErrorCode.UNAVAILABLE.value)
    deduplicated: dict[tuple[str, str, int], dict[str, Any]] = {}
    for item in results:
        key = (
            item["knowledge_resource_id"],
            item["doc_id"],
            item["chunk_index"],
        )
        if key not in deduplicated or item["score"] > deduplicated[key]["score"]:
            deduplicated[key] = item
    results = list(deduplicated.values())
    results.sort(key=lambda item: item["score"], reverse=True)
    results = results[:top_k]
    return _result(
        {
            "results": results,
            "total_hits": len(results),
            "returned_count": len(results),
            "max_score": results[0]["score"] if results else None,
            "successful_searches": successful,
            "failed_searches": exhaustive_plan.requested_count - successful,
            "partial_failures": partial_failures,
        }
    )


async def _single_resource_call(
    session: AsyncSession,
    user: User,
    knowledge_resource_id: str,
    operation: Callable[[PolarRAGClient, Any, dict[str, Any]], Any],
    *,
    tool_name: str,
    client_factory: ClientFactory,
    resource_scope: KnowledgeResourceScope | None = None,
    governor: PolarRAGToolGovernor | None = None,
) -> CallToolResult:
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            [knowledge_resource_id],
            resource_scope=resource_scope,
        )
    except KnowledgeAccessError as exc:
        return _error(exc.code.value)
    resource = plan.resources[0]
    governor = governor or get_polarrag_tool_governor()
    try:
        async with governor.reserve_instance(
            tool_name,
            plan.instance.id,
            1,
        ):
            payload = await operation(
                client_factory(plan.instance),
                resource,
                plan.acl_context,
            )
    except PolarRAGGovernanceError as exc:
        return _governance_error(exc)
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
    resource_scope: KnowledgeResourceScope | None = None,
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
        return _with_image_resources(await client.fetch_context(
            resource.space_id,
            doc_id,
            chunk_index=chunk_index,
            window_size=window_size,
            acl_context=acl_context,
        ))

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        tool_name="kb_fetch_context",
        client_factory=client_factory,
        resource_scope=resource_scope,
    )


async def handle_doc_list_chunks(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    offset: int = 0,
    limit: int = 100,
    resource_scope: KnowledgeResourceScope | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id or offset < 0 or limit < 1 or limit > 1000:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        await _authorized_document_info(
            client,
            resource,
            doc_id,
            acl_context,
        )
        return _with_image_resources(await client.list_document_chunks(
            resource.space_id,
            doc_id,
            offset=offset,
            limit=limit,
            acl_context=acl_context,
        ))

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        tool_name="doc_list_chunks",
        client_factory=client_factory,
        resource_scope=resource_scope,
    )


async def handle_doc_find_by_name(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_ids: list[str],
    filename: str,
    limit: int = 20,
    resource_scope: KnowledgeResourceScope | None = None,
    client_factory: ClientFactory = client_from_instance,
    governor: PolarRAGToolGovernor | None = None,
) -> CallToolResult:
    if not filename.strip() or limit < 1 or limit > 1000:
        return _error("INVALID_ARGUMENT")
    try:
        plan = await plan_knowledge_access(
            session,
            user,
            knowledge_resource_ids,
            resource_scope=resource_scope,
        )
    except KnowledgeAccessError as exc:
        return _error(exc.code.value)
    governor = governor or get_polarrag_tool_governor()
    try:
        async with governor.reserve_instance(
            "doc_find_by_name",
            plan.instance.id,
            len(plan.resources),
        ):
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
    except PolarRAGGovernanceError as exc:
        return _governance_error(exc)
    except PolarRAGUpstreamError as exc:
        return _error(exc.code.value)
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
        matches = sanitized.get("items") if isinstance(sanitized, dict) else None
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
    resource_scope: KnowledgeResourceScope | None = None,
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
            "source",
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
        tool_name="doc_status",
        client_factory=client_factory,
        resource_scope=resource_scope,
    )


async def handle_doc_recall(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    query: str,
    top_k: int = 10,
    resource_scope: KnowledgeResourceScope | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id or not query.strip() or top_k < 1 or top_k > 1000:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        return _with_image_resources(await client.recall_document(
            resource.space_id,
            doc_id,
            kb_id=resource.kb_id,
            query=query.strip(),
            top_k=top_k,
            acl_context=acl_context,
        ))

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        tool_name="doc_recall",
        client_factory=client_factory,
        resource_scope=resource_scope,
    )


async def handle_doc_get_original(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    resource_scope: KnowledgeResourceScope | None = None,
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
            for key in ("doc_id", "filename", "source", "file_type", "oss_path")
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
        tool_name="doc_get_original",
        client_factory=client_factory,
        resource_scope=resource_scope,
    )


async def handle_doc_delete(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    resource_scope: KnowledgeResourceScope | None = None,
    client_factory: ClientFactory = client_from_instance,
) -> CallToolResult:
    if not doc_id:
        return _error("INVALID_ARGUMENT")

    async def operation(client, resource, acl_context):
        require_pas_managed_resource(resource)
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
            key: payload[key] for key in ("doc_id", "task_id", "status") if key in payload}

    return await _single_resource_call(
        session,
        user,
        knowledge_resource_id,
        operation,
        tool_name="doc_delete",
        client_factory=client_factory,
        resource_scope=resource_scope,
    )


async def handle_doc_rechunk(
    session: AsyncSession,
    user: User,
    *,
    knowledge_resource_id: str,
    doc_id: str,
    chunk_strategy: str = "inherit",
    chunk_max_tokens: int | None = None,
    resource_scope: KnowledgeResourceScope | None = None,
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
        require_pas_managed_resource(resource)
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
        tool_name="doc_rechunk",
        client_factory=client_factory,
        resource_scope=resource_scope,
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
    resource_scope: KnowledgeResourceScope | None = None,
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
            resource_scope=resource_scope,
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
    resource_scope: KnowledgeResourceScope | None = None,
    object_store_factory=object_store_from_space,
    client_factory: ClientFactory = client_from_instance,
    governor: PolarRAGToolGovernor | None = None,
) -> CallToolResult:
    try:
        resource, payload = await complete_upload(
            session,
            user,
            agent_id=agent_id,
            upload_session_id=upload_session_id,
            resource_scope=resource_scope,
            object_store_factory=object_store_factory,
            client_factory=client_factory,
            governor=governor,
        )
    except PolarRAGGovernanceError as exc:
        return _governance_error(exc)
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
    _governance_audit_error.set(None)
    async with get_session_factory()() as session:
        agent_context = await current_agent_user_context(session)
        if require_agent_user_token and agent_context is None:
            return _error("USER_AGENT_TOKEN_REQUIRED")
        if agent_context is not None:
            user = agent_context.user
        else:
            token = get_access_token()
            if token is None or not token.subject:
                return _error("AUTH_REQUIRED")
            try:
                workspace = await resolve_mcp_workspace_context(
                    session,
                    token.subject,
                    access_token_agent_id(token),
                    personal=is_personal_access(token),
                )
            except MCPWorkspaceUnavailable as exc:
                return _error(exc.code)
            if workspace.user is None:
                return _error("AUTH_REQUIRED")
            user = workspace.user
        if agent_context is not None:
            resource_scope = await current_polarrag_resource_scope(
                session,
                context=agent_context,
            )
            agent_id = agent_context.agent.id
        elif workspace.agent is None:
            resource_scope = None
            agent_id = None
        else:
            resource_scope = await resolve_polarrag_resource_scope_for_agent(
                session,
                workspace.agent.id,
                user.id,
            )
            agent_id = workspace.agent.id
        governance_error: PolarRAGGovernanceError | None
        try:
            await get_polarrag_tool_governor().check_rate(
                tool_name,
                user.id,
                agent_id,
            )
        except PolarRAGGovernanceError as exc:
            governance_error = exc
            result = _governance_error(exc)
        else:
            governance_error = None
            if require_agent_user_token and agent_context is not None:
                kwargs["agent_id"] = agent_context.agent.id
            result = await handler(
                session,
                user,
                resource_scope=resource_scope,
                **kwargs,
            )
            governance_error = _governance_audit_error.get()
        error_code = None
        result_payload: dict[str, Any] = {}
        if result.content:
            try:
                decoded = json.loads(cast(TextContent, result.content[0]).text
                )
                if isinstance(decoded, dict):
                    result_payload = decoded
                candidate = result_payload.get("error")
                if isinstance(candidate, str):
                    error_code = candidate
            except (AttributeError, TypeError, ValueError):
                if result.isError:
                    error_code = "POLARRAG_TOOL_FAILED"
        governance_rejected = error_code == "POLARRAG_TOOL_LIMITED"
        resource_ids = [] if governance_rejected else kwargs.get("knowledge_resource_ids")
        if resource_ids is None:
            single_resource = kwargs.get("knowledge_resource_id")
            resource_ids = [single_resource] if single_resource is not None else []
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
                    if isinstance(item, dict) and isinstance(item.get("knowledge_resource_id"), str)
                ]
        normalized_resource_ids = resource_ids if isinstance(resource_ids, list) else []
        if governance_rejected:
            audit_info = {
                "polarrag_status": error_code,
                "governance_reason": result_payload.get("reason"),
                "retry_after_seconds": result_payload.get("retry_after_seconds"),
            }
            if governance_error is not None:
                if governance_error.requested_fanout is not None:
                    audit_info["requested_fanout"] = governance_error.requested_fanout
                if governance_error.current_inflight is not None:
                    audit_info["current_inflight"] = governance_error.current_inflight
        else:
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
            "Omit knowledge_resource_ids to search every knowledge base in the "
            "effective Agent and user scope, or pass opaque IDs returned by "
            "list_knowledge_resources. Results are merged and ranked across "
            "instances, Spaces, and KBs. Always inspect partial_failures because an unavailable "
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
            Field(min_length=1, max_length=1000),
        ] | None = None,
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
            "List ACL-authorized chunks from one PolarRAG document in chunk "
            "index order. Pass the knowledge_resource_id and doc_id from "
            "document discovery, then continue with offset and limit until "
            "the result has no more hits. Each chunk source includes "
            "image_resources when the source document contains extracted "
            "images."
        ),
        annotations=annotations,
    )
    async def doc_list_chunks(
        knowledge_resource_id: Annotated[str, Field(min_length=36, max_length=36)],
        doc_id: Annotated[str, Field(min_length=1, max_length=512)],
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> CallToolResult:
        return await _execute_tool(
            "doc_list_chunks",
            handle_doc_list_chunks,
            knowledge_resource_id=knowledge_resource_id,
            doc_id=doc_id,
            offset=offset,
            limit=limit,
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
            "complete_document_upload with only the upload_session_id. PAS derives the "
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
            "Complete a direct-to-OSS upload using only its upload_session_id. "
            "PAS lists and validates multipart parts with server-owned OSS access. "
            "If parts are missing, the result remains prepared and returns fresh "
            "URLs only for missing parts; upload them locally and call this tool again. "
            "When all parts exist, PAS finalizes multipart, rebuilds trusted identity, "
            "submits the object, and returns PolarRAG's authoritative doc_id and status."
        ),
        annotations=mutation_annotations,
    )
    async def complete_document_upload(
        upload_session_id: Annotated[
            str, Field(min_length=36, max_length=36)
        ],
    ) -> CallToolResult:
        return await _execute_tool(
            "complete_document_upload",
            handle_complete_document_upload,
            require_agent_user_token=True,
            upload_session_id=upload_session_id,
        )
