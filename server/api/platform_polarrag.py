from __future__ import annotations

from typing import Annotated, Any, Literal, cast

import json

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    Response,
    UploadFile,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.platform_oauth import (
    PlatformAccessContext,
    get_platform_access_context,
)
from server.api.polarrag_documents import (
    DocumentResourceRequest,
    FindDocumentsRequest,
    ListDocumentsRequest,
    _member_access,
    _upstream_error,
    delete_document,
    find_documents,
    list_documents,
    upload_document,
)
from server.config import get_config
from server.db.engine import get_session
from server.mcp.agent_user_context import (
    resolve_polarrag_resource_scope_for_agent,
)
from server.mcp.transport import _get_mcp_server
from server.models import (
    KnowledgeResource,
    KnowledgeResourceManagementMode,
    PolarRAGSpace,
)
from server.polarrag.access import list_visible_knowledge_resources_cursor_page
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import PolarRAGUpstreamError

router = APIRouter(prefix="/v1", tags=["platform-polarrag"])


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=10_000)
    search_mode: str = Field(default="balanced", min_length=1, max_length=32)
    top_k: int = Field(default=10, ge=1, le=1000)
    min_score: float | None = Field(default=None, ge=0)
    reranker: bool = False


class KnowledgeBaseSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    knowledge_resource_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=36,
    )
    space_id: str | None = Field(default=None, min_length=1, max_length=255)
    kb_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_identifier(self) -> "KnowledgeBaseSelector":
        if self.knowledge_resource_id is not None:
            if self.space_id is not None or self.kb_id is not None:
                raise ValueError("knowledge_resource_id cannot be combined with space_id or kb_id")
            return self
        if self.space_id is None or self.kb_id is None:
            raise ValueError("exactly one knowledge_resource_id or space_id and kb_id pair is required")
        return self


class KnowledgeBaseSearchRequest(SearchRequest):
    targets: list[KnowledgeBaseSelector] | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
    )


class DocumentFindByNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    targets: list[KnowledgeBaseSelector] = Field(min_length=1, max_length=50)
    filename: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=20, ge=1, le=1000)


class DocumentRechunkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    chunk_strategy: Literal["inherit", "hybrid", "hierarchical"] = "inherit"
    chunk_max_tokens: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_inherited_strategy(self) -> "DocumentRechunkRequest":
        if self.chunk_strategy == "inherit" and self.chunk_max_tokens is not None:
            raise ValueError("chunk_max_tokens requires an explicit chunk_strategy")
        return self


class DocumentBatchRechunkRequest(DocumentRechunkRequest):
    doc_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("doc_ids")
    @classmethod
    def validate_unique_document_ids(cls, doc_ids: list[str]) -> list[str]:
        if len(set(doc_ids)) != len(doc_ids):
            raise ValueError("doc_ids must not contain duplicates")
        return doc_ids


class DocumentUploadPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    filename: str = Field(min_length=1, max_length=512)
    file_size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    file_md5: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    file_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    content_type: str | None = Field(default=None, min_length=1, max_length=255)


class SpaceSearchRequest(SearchRequest):
    kb_ids: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
    )
    knowledge_resource_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
    )


class FlexibleObjectResponse(RootModel[dict[str, Any]]):
    pass


class KnowledgeBaseResponse(BaseModel):
    knowledge_resource_id: str
    knowledge_space_id: str
    space_id: str
    knowledge_space_name: str
    name: str
    kb_id: str
    kb_type: str
    usage: str | None
    upload_ready: bool


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]
    next_cursor: str | None


class SpaceResponse(BaseModel):
    knowledge_space_id: str
    space_id: str
    name: str


class DocumentListResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None


class DocumentChunkListResponse(BaseModel):
    items: list[dict[str, Any]]
    offset: int
    limit: int
    total: int | None
    next_offset: int | None


class SearchHitResponse(BaseModel):
    knowledge_resource_id: str | None
    doc_id: str | None
    chunk_index: int | None
    content: str | None
    score: float | None
    image_resources: list[dict[str, Any]]
    metadata: dict[str, Any]


class SearchResponse(BaseModel):
    items: list[SearchHitResponse]
    top_k: int
    partial_failures: list[dict[str, Any]]


class ToolListResponse(BaseModel):
    tools: list[dict[str, Any]]
    total: int
    offset: int
    limit: int


def _set_auth_context(context: PlatformAccessContext):
    return auth_context_var.set(AuthenticatedUser(context.access_token))


async def _invoke_tool(
    context: PlatformAccessContext,
    name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    token = _set_auth_context(context)
    try:
        mcp = _get_mcp_server()
        visible_names = {tool.name for tool in await mcp.list_tools()}
        if name not in visible_names:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "TOOL_NOT_AVAILABLE",
                    "message": "Tool is not available.",
                },
            )
        result = await mcp.call_tool(name, arguments)
    except ToolError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "TOOL_CALL_FAILED", "message": str(exc)},
        ) from exc
    finally:
        auth_context_var.reset(token)
    if not isinstance(result, CallToolResult):
        content = result[0] if isinstance(result, tuple) else result
        return CallToolResult(content=cast(list[ContentBlock], content), isError=False)
    return result


def _tool_payload(result: CallToolResult) -> dict[str, Any]:
    if not result.content:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid response.",
            },
        )
    try:
        payload = json.loads(cast(Any, result.content[0]).text)
    except (AttributeError, TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid response.",
            },
        )
    if result.isError:
        code = str(payload.get("error", "POLARRAG_OPERATION_FAILED"))
        status_code = {
            "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE": 404,
            "DOCUMENT_NOT_ACCESSIBLE": 404,
            "NO_ACCESSIBLE_RESOURCE": 404,
            "DOCUMENT_PERMISSION_DENIED": 403,
            "IDENTITY_CONTEXT_UNAVAILABLE": 409,
            "RERANKER_NOT_CONFIGURED": 409,
            "EXTERNAL_SYNC_RESOURCE_READ_ONLY": 409,
            "POLARRAG_TOOL_LIMITED": 429,
            "POLARRAG_UNAVAILABLE": 503,
        }.get(code, 422)
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": payload.get("message", code)},
        )
    return payload


def _tool_result_object(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid response.",
            },
        )
    return result


def _knowledge_resource_item(resource: Any) -> dict[str, Any]:
    return {
        "knowledge_resource_id": resource.id,
        "knowledge_space_id": resource.knowledge_space_id,
        "space_id": resource.space_id,
        "knowledge_space_name": resource.space.name,
        "name": resource.name,
        "kb_id": resource.kb_id,
        "kb_type": resource.kb_type,
        "usage": resource.usage,
        "upload_ready": bool(
            resource.management_mode == KnowledgeResourceManagementMode.NATIVE
            and resource.space.oss_config_validated
            and resource.space.oss_bucket
            and resource.space.oss_endpoint
            and resource.space.oss_object_prefix
            and resource.space.oss_access_key_id_ciphertext
            and resource.space.oss_access_key_secret_ciphertext
        ),
    }


async def _visible_resource_page(
    context: PlatformAccessContext,
    session: AsyncSession,
    *,
    limit: int,
    cursor: str | None = None,
    knowledge_space_id: str | None = None,
    resource_id: str | None = None,
) -> tuple[list[Any], bool, bool]:
    scope = await resolve_polarrag_resource_scope_for_agent(
        session,
        context.agent.id,
        context.user.id,
    )
    return await list_visible_knowledge_resources_cursor_page(
        session,
        context.user,
        resource_scope=scope,
        knowledge_space_id=knowledge_space_id,
        resource_id=resource_id,
        cursor=cursor,
        limit=limit,
    )


def _not_accessible_resource() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
            "message": "Knowledge resource is not accessible.",
        },
    )


def _ambiguous_space() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "SPACE_ID_AMBIGUOUS",
            "message": "Space ID resolves to multiple PolarRAG instances.",
        },
    )


async def _resolve_space(
    session: AsyncSession,
    space_id: str,
) -> PolarRAGSpace:
    spaces = list((await session.execute(select(PolarRAGSpace).where(PolarRAGSpace.space_id == space_id))).scalars())
    if len(spaces) > 1:
        raise _ambiguous_space()
    if not spaces:
        raise _not_accessible_resource()
    return spaces[0]


async def _resolve_space_knowledge_base(
    session: AsyncSession,
    *,
    space_id: str,
    kb_id: str,
) -> str:
    space = await _resolve_space(session, space_id)
    resources = list(
        (
            await session.execute(
                select(KnowledgeResource.id).where(
                    KnowledgeResource.knowledge_space_id == space.knowledge_space_id,
                    KnowledgeResource.kb_id == kb_id,
                )
            )
        ).scalars()
    )
    if len(resources) > 1:
        raise _ambiguous_space()
    if not resources:
        raise _not_accessible_resource()
    return resources[0]


async def _resolve_search_targets(
    session: AsyncSession,
    targets: list[KnowledgeBaseSelector],
    *,
    expected_space_id: str | None = None,
) -> list[str]:
    resource_ids: list[str] = []
    for target in targets:
        if target.knowledge_resource_id is not None:
            if expected_space_id is not None:
                resource = await session.get(
                    KnowledgeResource,
                    target.knowledge_resource_id,
                )
                if resource is not None and resource.space_id != expected_space_id:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "KNOWLEDGE_RESOURCE_OUTSIDE_SPACE",
                            "message": "Knowledge resource does not belong to the requested Space.",
                        },
                    )
            resource_ids.append(target.knowledge_resource_id)
            continue
        assert target.space_id is not None
        assert target.kb_id is not None
        if expected_space_id is not None and target.space_id != expected_space_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "KNOWLEDGE_RESOURCE_OUTSIDE_SPACE",
                    "message": "Knowledge resource does not belong to the requested Space.",
                },
            )
        resource_ids.append(
            await _resolve_space_knowledge_base(
                session,
                space_id=target.space_id,
                kb_id=target.kb_id,
            )
        )
    return list(dict.fromkeys(resource_ids))


def _resource_page_response(
    resources: list[Any],
    *,
    has_more: bool,
) -> dict[str, Any]:
    return {
        "items": [_knowledge_resource_item(resource) for resource in resources],
        "next_cursor": resources[-1].id if has_more and resources else None,
    }


@router.get("/knowledge-bases", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    resources, has_more, cursor_valid = await _visible_resource_page(
        context,
        session,
        limit=limit,
        cursor=cursor,
    )
    if not cursor_valid:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_CURSOR", "message": "Cursor is invalid."},
        )
    return _resource_page_response(resources, has_more=has_more)


@router.get("/spaces/{space_id}", response_model=SpaceResponse)
async def get_space(
    space_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    space = await _resolve_space(session, space_id)
    resources, _has_more, _cursor_valid = await _visible_resource_page(
        context,
        session,
        knowledge_space_id=space.knowledge_space_id,
        limit=1,
    )
    if not resources:
        raise _not_accessible_resource()
    return {
        "knowledge_space_id": space.knowledge_space_id,
        "space_id": space.space_id,
        "name": space.name,
    }


@router.get(
    "/spaces/{space_id}/knowledge-bases",
    response_model=KnowledgeBaseListResponse,
)
async def list_space_knowledge_bases(
    space_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    space = await _resolve_space(session, space_id)
    resources, has_more, cursor_valid = await _visible_resource_page(
        context,
        session,
        knowledge_space_id=space.knowledge_space_id,
        limit=limit,
        cursor=cursor,
    )
    if not cursor_valid:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_CURSOR", "message": "Cursor is invalid."},
        )
    if not resources and cursor is None:
        raise _not_accessible_resource()
    return _resource_page_response(resources, has_more=has_more)


@router.get(
    "/spaces/{space_id}/knowledge-bases/{kb_id}",
    response_model=KnowledgeBaseResponse,
)
async def get_space_knowledge_base(
    space_id: str,
    kb_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    resources, _has_more, _cursor_valid = await _visible_resource_page(
        context,
        session,
        resource_id=knowledge_resource_id,
        limit=1,
    )
    resource = resources[0] if resources else None
    if resource is None:
        raise _not_accessible_resource()
    return _knowledge_resource_item(resource)


@router.get(
    "/knowledge-bases/{knowledge_resource_id}",
    response_model=KnowledgeBaseResponse,
)
async def get_knowledge_base(
    knowledge_resource_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    resources, _has_more, _cursor_valid = await _visible_resource_page(
        context,
        session,
        resource_id=knowledge_resource_id,
        limit=1,
    )
    resource = resources[0] if resources else None
    if resource is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
                "message": "Knowledge resource is not accessible.",
            },
        )
    return _knowledge_resource_item(resource)


@router.get(
    "/knowledge-bases/{knowledge_resource_id}/documents",
    response_model=DocumentListResponse,
)
async def list_knowledge_base_documents(
    knowledge_resource_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    filename: str | None = Query(default=None, min_length=1, max_length=1024),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    if filename is not None:
        payload = await find_documents(
            FindDocumentsRequest(
                agent_id=context.agent.id,
                knowledge_resource_id=knowledge_resource_id,
                filename=filename,
                limit=limit,
            ),
            user=context.user,
            session=session,
        )
        return {"items": payload["documents"], "next_cursor": None}
    payload = await list_documents(
        ListDocumentsRequest(
            agent_id=context.agent.id,
            knowledge_resource_id=knowledge_resource_id,
            size=limit,
            after_doc_id=cursor,
        ),
        user=context.user,
        session=session,
    )
    return {
        "items": payload["documents"],
        "next_cursor": payload.get("next_after_doc_id"),
    }


@router.post(
    "/knowledge-bases/{knowledge_resource_id}/documents",
    response_model=FlexibleObjectResponse,
    status_code=201,
)
async def upload_knowledge_base_document(
    knowledge_resource_id: str,
    file: UploadFile = File(...),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    return await upload_document(
        agent_id=context.agent.id,
        knowledge_resource_id=knowledge_resource_id,
        file=file,
        user=context.user,
        session=session,
    )


@router.post("/documents/_find", response_model=FlexibleObjectResponse)
async def find_knowledge_base_documents(
    body: DocumentFindByNameRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_ids = await _resolve_search_targets(session, body.targets)
    return _tool_payload(
        await _invoke_tool(
            context,
            "doc_find_by_name",
            {
                "knowledge_resource_ids": knowledge_resource_ids,
                "filename": body.filename,
                "limit": body.limit,
            },
        )
    )


@router.post(
    "/knowledge-bases/{knowledge_resource_id}/document-uploads",
    response_model=FlexibleObjectResponse,
    status_code=201,
)
async def prepare_knowledge_base_document_upload(
    knowledge_resource_id: str,
    body: DocumentUploadPrepareRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    return _tool_result_object(
        _tool_payload(
            await _invoke_tool(
                context,
                "prepare_document_upload",
                {
                    "knowledge_resource_id": knowledge_resource_id,
                    **body.model_dump(),
                },
            )
        )
    )


@router.post(
    "/document-uploads/{upload_session_id}/complete",
    response_model=FlexibleObjectResponse,
)
async def complete_knowledge_base_document_upload(
    upload_session_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    return _tool_result_object(
        _tool_payload(
            await _invoke_tool(
                context,
                "complete_document_upload",
                {"upload_session_id": upload_session_id},
            )
        )
    )


@router.get(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}",
    response_model=FlexibleObjectResponse,
)
async def get_knowledge_base_document(
    knowledge_resource_id: str,
    doc_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    access = await _member_access(
        session,
        context.user,
        context.agent.id,
        knowledge_resource_id,
    )
    resource = access.resources[0]
    try:
        payload = await client_from_instance(access.instance).document_info(
            resource.space_id,
            doc_id,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    if (
        payload.get("doc_id") != doc_id
        or payload.get("space_id") != resource.space_id
        or payload.get("kb_id") != resource.kb_id
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DOCUMENT_NOT_ACCESSIBLE",
                "message": "Document is not accessible.",
            },
        )
    allowed = {
        "doc_id",
        "kb_id",
        "filename",
        "source",
        "file_size_bytes",
        "created_at",
        "updated_at",
        "completed_at",
        "status",
        "chunk_count",
        "active_generation",
        "revision_status",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _image_resources(source: dict[str, Any]) -> list[dict[str, Any]]:
    resources = source.get("image_resources")
    if not isinstance(resources, list):
        return []
    return [dict(resource) for resource in resources if isinstance(resource, dict)]


def _chunk_list_response(
    result: dict[str, Any],
    *,
    doc_id: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    hits = result.get("hits")
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(raw_hits, list):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid chunk response.",
            },
        )
    items = []
    for raw_hit in raw_hits:
        source = raw_hit.get("_source") if isinstance(raw_hit, dict) else None
        if not isinstance(source, dict) or source.get("doc_id") != doc_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "POLARRAG_INVALID_RESPONSE",
                    "message": "PolarRAG returned an invalid chunk response.",
                },
            )
        item = dict(source)
        item["image_resources"] = _image_resources(source)
        items.append(item)
    total_value = hits.get("total") if isinstance(hits, dict) else None
    if isinstance(total_value, int) and not isinstance(total_value, bool):
        total = total_value
    elif isinstance(total_value, dict) and isinstance(total_value.get("value"), int):
        total = total_value["value"]
    else:
        total = None
    next_offset = offset + len(items)
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "total": total,
        "next_offset": next_offset if total is not None and next_offset < total else None,
    }


@router.get(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks",
    response_model=DocumentChunkListResponse,
)
async def list_knowledge_base_document_chunks(
    knowledge_resource_id: str,
    doc_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    payload = _tool_payload(
        await _invoke_tool(
            context,
            "doc_list_chunks",
            {
                "knowledge_resource_id": knowledge_resource_id,
                "doc_id": doc_id,
                "offset": offset,
                "limit": limit,
            },
        )
    )
    response = _chunk_list_response(
        cast(dict[str, Any], payload["result"]),
        doc_id=doc_id,
        offset=offset,
        limit=limit,
    )
    response["total"] = None
    response["next_offset"] = None
    if len(response["items"]) < limit:
        return response
    probe_payload = _tool_payload(
        await _invoke_tool(
            context,
            "doc_list_chunks",
            {
                "knowledge_resource_id": knowledge_resource_id,
                "doc_id": doc_id,
                "offset": offset + limit,
                "limit": 1,
            },
        )
    )
    probe_response = _chunk_list_response(
        cast(dict[str, Any], probe_payload["result"]),
        doc_id=doc_id,
        offset=offset + limit,
        limit=1,
    )
    if probe_response["items"]:
        response["next_offset"] = offset + limit
    return response


@router.get(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context",
    response_model=FlexibleObjectResponse,
)
async def get_knowledge_base_document_chunk_context(
    knowledge_resource_id: str,
    doc_id: str,
    chunk_index: int = Path(ge=0),
    window_size: int = Query(default=2, ge=0, le=100),
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    return _tool_result_object(
        _tool_payload(
            await _invoke_tool(
                context,
                "kb_fetch_context",
                {
                    "knowledge_resource_id": knowledge_resource_id,
                    "doc_id": doc_id,
                    "chunk_index": chunk_index,
                    "window_size": window_size,
                },
            )
        )
    )


@router.get(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/original",
    response_model=FlexibleObjectResponse,
)
async def get_knowledge_base_document_original(
    knowledge_resource_id: str,
    doc_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    payload = _tool_payload(
        await _invoke_tool(
            context,
            "doc_get_original",
            {
                "knowledge_resource_id": knowledge_resource_id,
                "doc_id": doc_id,
            },
        )
    )
    result = cast(dict[str, Any], payload["result"])
    if "size" in result and "file_size_bytes" not in result:
        result["file_size_bytes"] = result.pop("size")
    return result


@router.delete(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}",
    response_model=FlexibleObjectResponse,
    status_code=202,
)
async def delete_knowledge_base_document(
    knowledge_resource_id: str,
    doc_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    return await delete_document(
        doc_id=doc_id,
        body=DocumentResourceRequest(
            agent_id=context.agent.id,
            knowledge_resource_id=knowledge_resource_id,
        ),
        user=context.user,
        session=session,
    )


@router.post(
    "/knowledge-bases/{knowledge_resource_id}/documents/rechunk",
    response_model=FlexibleObjectResponse,
    status_code=202,
    responses={207: {"model": FlexibleObjectResponse, "description": "Partial success"}},
)
async def batch_rechunk_knowledge_base_documents(
    knowledge_resource_id: str,
    body: DocumentBatchRechunkRequest,
    response: Response,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    accepted = []
    failed = []
    rechunk_body = DocumentRechunkRequest(
        **body.model_dump(exclude={"doc_ids"})
    )
    for doc_id in body.doc_ids:
        try:
            result = await rechunk_knowledge_base_document(
                knowledge_resource_id,
                doc_id,
                rechunk_body,
                context,
            )
        except HTTPException as exc:
            detail: dict[str, Any] = (
                cast(dict[str, Any], exc.detail)
                if isinstance(exc.detail, dict)
                else {}
            )
            failed.append(
                {
                    "doc_id": doc_id,
                    "status_code": exc.status_code,
                    "code": str(detail.get("code", "DOCUMENT_RECHUNK_FAILED")),
                    "message": str(detail.get("message", "Document rechunk failed.")),
                }
            )
        else:
            accepted.append({**result, "doc_id": doc_id})

    if failed:
        response.status_code = 207
    else:
        response.status_code = 202
    return {"accepted": accepted, "failed": failed}


@router.post(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk",
    response_model=FlexibleObjectResponse,
    status_code=202,
)
async def rechunk_knowledge_base_document(
    knowledge_resource_id: str,
    doc_id: str,
    body: DocumentRechunkRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    return _tool_result_object(
        _tool_payload(
            await _invoke_tool(
                context,
                "doc_rechunk",
                {
                    "knowledge_resource_id": knowledge_resource_id,
                    "doc_id": doc_id,
                    **body.model_dump(),
                },
            )
        )
    )


@router.get(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents",
    response_model=DocumentListResponse,
)
async def list_space_knowledge_base_documents(
    space_id: str,
    kb_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    filename: str | None = Query(default=None, min_length=1, max_length=1024),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await list_knowledge_base_documents(
        knowledge_resource_id,
        limit,
        cursor,
        filename,
        context,
        session,
    )


@router.post(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents",
    response_model=FlexibleObjectResponse,
    status_code=201,
)
async def upload_space_knowledge_base_document(
    space_id: str,
    kb_id: str,
    file: UploadFile = File(...),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await upload_knowledge_base_document(
        knowledge_resource_id,
        file,
        context,
        session,
    )


@router.post(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/document-uploads",
    response_model=FlexibleObjectResponse,
    status_code=201,
)
async def prepare_space_knowledge_base_document_upload(
    space_id: str,
    kb_id: str,
    body: DocumentUploadPrepareRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await prepare_knowledge_base_document_upload(
        knowledge_resource_id,
        body,
        context,
    )


@router.get(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}",
    response_model=FlexibleObjectResponse,
)
async def get_space_knowledge_base_document(
    space_id: str,
    kb_id: str,
    doc_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await get_knowledge_base_document(
        knowledge_resource_id,
        doc_id,
        context,
        session,
    )


@router.get(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks",
    response_model=DocumentChunkListResponse,
)
async def list_space_knowledge_base_document_chunks(
    space_id: str,
    kb_id: str,
    doc_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await list_knowledge_base_document_chunks(
        knowledge_resource_id,
        doc_id,
        offset,
        limit,
        context,
    )


@router.get(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks/{chunk_index}/context",
    response_model=FlexibleObjectResponse,
)
async def get_space_knowledge_base_document_chunk_context(
    space_id: str,
    kb_id: str,
    doc_id: str,
    chunk_index: int = Path(ge=0),
    window_size: int = Query(default=2, ge=0, le=100),
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await get_knowledge_base_document_chunk_context(
        knowledge_resource_id,
        doc_id,
        chunk_index,
        window_size,
        context,
    )


@router.get(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/original",
    response_model=FlexibleObjectResponse,
)
async def get_space_knowledge_base_document_original(
    space_id: str,
    kb_id: str,
    doc_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await get_knowledge_base_document_original(
        knowledge_resource_id,
        doc_id,
        context,
    )


@router.delete(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}",
    response_model=FlexibleObjectResponse,
    status_code=202,
)
async def delete_space_knowledge_base_document(
    space_id: str,
    kb_id: str,
    doc_id: str,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await delete_knowledge_base_document(
        knowledge_resource_id,
        doc_id,
        context,
        session,
    )


@router.post(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk",
    response_model=FlexibleObjectResponse,
    status_code=202,
    responses={207: {"model": FlexibleObjectResponse, "description": "Partial success"}},
)
async def batch_rechunk_space_knowledge_base_documents(
    space_id: str,
    kb_id: str,
    body: DocumentBatchRechunkRequest,
    response: Response,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await batch_rechunk_knowledge_base_documents(
        knowledge_resource_id,
        body,
        response,
        context,
    )


@router.post(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/rechunk",
    response_model=FlexibleObjectResponse,
    status_code=202,
)
async def rechunk_space_knowledge_base_document(
    space_id: str,
    kb_id: str,
    doc_id: str,
    body: DocumentRechunkRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await rechunk_knowledge_base_document(
        knowledge_resource_id,
        doc_id,
        body,
        context,
    )


def _search_response(payload: dict[str, Any], top_k: int) -> dict[str, Any]:
    items = []
    for hit in payload.get("results", []):
        metadata = {
            "page_numbers": hit.get("page_numbers", []),
            "headings": hit.get("headings", []),
            "captions": hit.get("captions", []),
            **hit.get("metadata_hints", {}),
        }
        items.append(
            {
                "knowledge_resource_id": hit.get("knowledge_resource_id"),
                "doc_id": hit.get("doc_id"),
                "chunk_index": hit.get("chunk_index"),
                "content": hit.get("text"),
                "score": hit.get("score"),
                "image_resources": _image_resources(hit),
                "metadata": metadata,
            }
        )
    return {
        "items": items,
        "top_k": top_k,
        "partial_failures": payload.get("partial_failures", []),
    }


def _document_search_results(
    result: dict[str, Any],
    *,
    knowledge_resource_id: str,
    doc_id: str,
) -> list[dict[str, Any]]:
    hits = result.get("hits")
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(raw_hits, list):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid search response.",
            },
        )
    normalized = []
    for raw_hit in raw_hits:
        source = raw_hit.get("_source") if isinstance(raw_hit, dict) else None
        if not isinstance(source, dict) or source.get("doc_id") != doc_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "POLARRAG_INVALID_RESPONSE",
                    "message": "PolarRAG returned an invalid search response.",
                },
            )
        normalized.append(
            {
                "knowledge_resource_id": knowledge_resource_id,
                "doc_id": doc_id,
                "chunk_index": source.get("chunk_index"),
                "text": source.get("text"),
                "score": raw_hit.get("_score", 0.0),
                "page_numbers": source.get("page_numbers", []),
                "headings": source.get("headings", []),
                "captions": source.get("captions", []),
                "image_resources": _image_resources(source),
                "metadata_hints": (
                    source["metadata"]
                    if isinstance(source.get("metadata"), dict)
                    else {}
                ),
            }
        )
    return normalized


async def _search_selected_knowledge_bases(
    context: PlatformAccessContext,
    body: SearchRequest,
    knowledge_resource_ids: list[str] | None,
) -> dict[str, Any]:
    payload = _tool_payload(
        await _invoke_tool(
            context,
            "kb_search",
            {
                "query": body.query,
                "knowledge_resource_ids": knowledge_resource_ids,
                "search_mode": body.search_mode,
                "top_k": body.top_k,
                "min_score": body.min_score,
                "reranker": body.reranker,
            },
        )
    )
    return _search_response(payload, body.top_k)


@router.post("/knowledge-bases/search", response_model=SearchResponse)
async def search_knowledge_bases(
    body: KnowledgeBaseSearchRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_ids = (
        await _resolve_search_targets(session, body.targets)
        if body.targets is not None
        else None
    )
    return await _search_selected_knowledge_bases(
        context,
        body,
        knowledge_resource_ids,
    )


@router.post("/spaces/{space_id}/search", response_model=SearchResponse)
async def search_space_knowledge_bases(
    space_id: str,
    body: SpaceSearchRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    targets = [
        KnowledgeBaseSelector(space_id=space_id, kb_id=kb_id)
        for kb_id in body.kb_ids or []
    ] + [
        KnowledgeBaseSelector(knowledge_resource_id=knowledge_resource_id)
        for knowledge_resource_id in body.knowledge_resource_ids or []
    ]
    if targets:
        knowledge_resource_ids = await _resolve_search_targets(
            session,
            targets,
            expected_space_id=space_id,
        )
    else:
        space = await _resolve_space(session, space_id)
        resource_scope = await resolve_polarrag_resource_scope_for_agent(
            session,
            context.agent.id,
            context.user.id,
        )
        max_resources = (
            get_config().polarrag_tool_limits.max_exhaustive_knowledge_resources
        )
        resources, has_more, _cursor_valid = (
            await list_visible_knowledge_resources_cursor_page(
                session,
                context.user,
                resource_scope=resource_scope,
                knowledge_space_id=space.knowledge_space_id,
                limit=max_resources,
            )
        )
        if has_more:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TOO_MANY_KNOWLEDGE_RESOURCES",
                    "message": "Visible knowledge resources exceed the configured limit.",
                },
            )
        knowledge_resource_ids = [resource.id for resource in resources]
    return await _search_selected_knowledge_bases(
        context,
        body,
        knowledge_resource_ids,
    )


@router.post(
    "/knowledge-bases/{knowledge_resource_id}/search",
    response_model=SearchResponse,
)
async def search_knowledge_base(
    knowledge_resource_id: str,
    body: SearchRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    return await _search_selected_knowledge_bases(
        context,
        body,
        [knowledge_resource_id],
    )


@router.post(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/search",
    response_model=SearchResponse,
)
async def search_space_knowledge_base(
    space_id: str,
    kb_id: str,
    body: SearchRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await search_knowledge_base(
        knowledge_resource_id,
        body,
        context,
    )


@router.post(
    "/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/search",
    response_model=SearchResponse,
)
async def search_knowledge_base_document(
    knowledge_resource_id: str,
    doc_id: str,
    body: SearchRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    payload = _tool_payload(
        await _invoke_tool(
            context,
            "doc_recall",
            {
                "knowledge_resource_id": knowledge_resource_id,
                "doc_id": doc_id,
                "query": body.query,
                "top_k": body.top_k,
            },
        )
    )
    result = cast(dict[str, Any], payload["result"])
    return _search_response(
        {
            "results": _document_search_results(
                result,
                knowledge_resource_id=knowledge_resource_id,
                doc_id=doc_id,
            ),
            "partial_failures": [],
        },
        body.top_k,
    )


@router.post(
    "/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/search",
    response_model=SearchResponse,
)
async def search_space_knowledge_base_document(
    space_id: str,
    kb_id: str,
    doc_id: str,
    body: SearchRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
    session: AsyncSession = Depends(get_session),
):
    knowledge_resource_id = await _resolve_space_knowledge_base(
        session,
        space_id=space_id,
        kb_id=kb_id,
    )
    return await search_knowledge_base_document(
        knowledge_resource_id,
        doc_id,
        body,
        context,
    )


@router.get("/tools", response_model=ToolListResponse)
async def list_tools(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    token = _set_auth_context(context)
    try:
        tools = await _get_mcp_server().list_tools()
    finally:
        auth_context_var.reset(token)
    if search and search.strip():
        query = search.strip().casefold()
        tools = [tool for tool in tools if query in tool.name.casefold()]
    page = tools[offset : offset + limit]
    return {
        "tools": [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in page
        ],
        "total": len(tools),
        "offset": offset,
        "limit": limit,
    }


@router.post("/tools/call", response_model=FlexibleObjectResponse)
async def call_tool(
    body: ToolCallRequest,
    context: PlatformAccessContext = Depends(get_platform_access_context),
):
    result = await _invoke_tool(context, body.name, body.arguments)
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
