from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user
from server.core.agent_access import has_agent_access
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import Agent, AgentStatus, AuditStatus, KnowledgeResource, User, UserRole
from server.mcp.agent_user_context import (
    resolve_polarrag_resource_scope_for_agent,
)
from server.polarrag.access import (
    KnowledgeAccessError,
    KnowledgeAccessErrorCode,
    KnowledgeAccessPlan,
    plan_knowledge_access,
)
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import (
    PolarRAGClient,
    PolarRAGErrorCode,
    PolarRAGUpstreamError,
)
from server.polarrag.upload import (
    checksum_file,
    document_actor,
    document_object_key,
    file_type_from_filename,
    new_upload_object_id,
    object_store_from_space,
    validate_filename,
)

router = APIRouter(prefix="/me/polarrag", tags=["polarrag-documents"])


class DocumentResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: str = Field(min_length=1, max_length=36)
    knowledge_resource_id: str = Field(min_length=1, max_length=512)


class FindDocumentsRequest(DocumentResourceRequest):
    filename: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=20, ge=1, le=1000)


class ListDocumentsRequest(DocumentResourceRequest):
    size: int = Field(default=20, ge=1, le=200)
    after_doc_id: str | None = Field(default=None, min_length=1, max_length=512)


class RechunkDocumentRequest(DocumentResourceRequest):
    chunk_strategy: Literal["inherit", "hybrid", "hierarchical"] = "inherit"
    chunk_max_tokens: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_inherited_strategy(self) -> RechunkDocumentRequest:
        if self.chunk_strategy == "inherit" and self.chunk_max_tokens is not None:
            raise ValueError("chunk_max_tokens requires an explicit chunk_strategy")
        return self


async def _member_access(
    session: AsyncSession,
    user: User,
    agent_id: str,
    knowledge_resource_id: str,
) -> KnowledgeAccessPlan:
    if user.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_DOCUMENT_MANAGEMENT_FORBIDDEN",
                "message": "Administrators cannot manage PolarRAG documents.",
            },
        )
    try:
        agent = await session.get(Agent, agent_id)
        if (
            agent is None
            or agent.status != AgentStatus.ACTIVE
            or not await has_agent_access(session, agent_id, user.id)
        ):
            raise KnowledgeAccessError(
                KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE
            )
        resource_scope = await resolve_polarrag_resource_scope_for_agent(
            session, agent_id
        )
        return await plan_knowledge_access(
            session,
            user,
            [knowledge_resource_id],
            resource_scope=resource_scope,
        )
    except KnowledgeAccessError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
                "message": "Knowledge resource is not accessible.",
            },
        ) from None


def _upstream_error(exc: PolarRAGUpstreamError) -> HTTPException:
    if exc.status_code == 403:
        return HTTPException(
            status_code=403,
            detail={
                "code": "DOCUMENT_PERMISSION_DENIED",
                "message": "PolarRAG denied the required document permission.",
            },
        )
    if exc.code == PolarRAGErrorCode.DOCUMENT_NOT_ACCESSIBLE:
        return HTTPException(
            status_code=404,
            detail={
                "code": "DOCUMENT_NOT_ACCESSIBLE",
                "message": "Document is not accessible.",
            },
        )
    if exc.status_code == 409:
        return HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_DOCUMENT_CONFLICT",
                "message": "The document is busy with another operation.",
            },
        )
    if exc.code == PolarRAGErrorCode.UNAVAILABLE:
        return HTTPException(
            status_code=503,
            detail={
                "code": "POLARRAG_UNAVAILABLE",
                "message": "PolarRAG is temporarily unavailable.",
            },
        )
    return HTTPException(
        status_code=502,
        detail={
            "code": "POLARRAG_DOCUMENT_OPERATION_FAILED",
            "message": "PolarRAG did not accept the document operation.",
        },
    )


async def _authorized_document(
    client: PolarRAGClient,
    resource: KnowledgeResource,
    doc_id: str,
    acl_context: dict[str, Any],
) -> None:
    try:
        document = await client.document_info(
            resource.space_id,
            doc_id,
            acl_context=acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    if (
        document.get("doc_id") != doc_id
        or document.get("space_id") != resource.space_id
        or document.get("kb_id") != resource.kb_id
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "DOCUMENT_NOT_ACCESSIBLE",
                "message": "Document is not accessible.",
            },
        )


def _visible_documents(
    payload: dict[str, Any],
    expected_kb_id: str,
) -> list[dict[str, Any]]:
    hits = payload.get("hits")
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(raw_hits, list):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid document response.",
            },
        )
    allowed = (
        "doc_id",
        "kb_id",
        "filename",
        "file_size_bytes",
        "created_at",
        "status",
        "chunk_count",
        "active_generation",
        "revision_status",
    )
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in raw_hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict) or source.get("kb_id") != expected_kb_id:
            continue
        doc_id = source.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        documents.append({key: source[key] for key in allowed if key in source})
    return documents


def _audit_context(
    resource: KnowledgeResource,
    **extra: Any,
) -> str:
    return json.dumps(
        {
            "knowledge_resource_ids": [resource.id],
            "polarrag_instance_ids": [resource.polarrag_instance_id],
            "space_ids": [resource.space_id],
            "kb_ids": [resource.kb_id],
            "polarrag_status": "success",
            **extra,
        },
        separators=(",", ":"),
    )


@router.post("/documents", status_code=201)
async def upload_document(
    agent_id: str = Form(...),
    knowledge_resource_id: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_DOCUMENT_UPLOAD_FORBIDDEN",
                "message": "Administrators cannot upload PolarRAG documents.",
            },
        )
    access = await _member_access(
        session, user, agent_id, knowledge_resource_id
    )
    resource = access.resources[0]
    space = access.space
    if (
        not space.oss_config_validated
        or not space.oss_bucket
        or not space.oss_object_prefix
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_OSS_NOT_CONFIGURED",
                "message": "The Space OSS upload configuration is not ready.",
            },
        )
    try:
        filename = validate_filename(file.filename)
        file_type = file_type_from_filename(filename)
        file_md5, file_sha256, file_size = await asyncio.to_thread(
            checksum_file,
            file.file,
        )
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_UPLOAD_FILE",
                "message": "The upload file is not valid.",
            },
        ) from None
    try:
        actor = document_actor(access.acl_context)
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDENTITY_CONTEXT_UNAVAILABLE",
                "message": "The trusted enterprise identity is unavailable.",
            },
        ) from None
    acl_context = dict(access.acl_context)
    acl_context["actor"] = actor
    upload_object_id = new_upload_object_id()
    key = document_object_key(
        space.oss_object_prefix,
        upload_object_id,
        filename,
    )
    try:
        store = object_store_from_space(space)
    except Exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_OSS_NOT_CONFIGURED",
                "message": "The Space OSS upload configuration is not ready.",
            },
        ) from None
    try:
        await store.put(key, file.file)
    except Exception:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "OSS_UPLOAD_FAILED",
                "message": "The document could not be uploaded to OSS.",
            },
        ) from None
    try:
        submitted = await client_from_instance(access.instance).submit_document(
            space.space_id,
            resource.kb_id,
            oss_path=f"oss://{space.oss_bucket}/{key}",
            filename=filename,
            file_type=file_type,
            file_md5=file_md5,
            file_size_bytes=file_size,
            metadata={"sha256": file_sha256},
            acl_context=acl_context,
        )
    except Exception:
        try:
            await store.delete(key)
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
                "message": "PolarRAG did not accept the uploaded document.",
            },
        ) from None
    await log_audit(
        session,
        user_id=user.id,
        action="polarrag.doc_upload",
        target_type="knowledge_resource",
        target_id=resource.id,
        status=AuditStatus.SUCCESS,
        client_info=_audit_context(
            resource,
            agent_id=agent_id,
            filename=filename,
            file_size_bytes=file_size,
        ),
        required=True,
    )
    return {
        "doc_id": submitted["doc_id"],
        "status": submitted.get("status", "DISPATCHED"),
        "filename": filename,
    }


@router.post("/documents/_find")
async def find_documents(
    body: FindDocumentsRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    access = await _member_access(
        session,
        user,
        body.agent_id,
        body.knowledge_resource_id,
    )
    resource = access.resources[0]
    try:
        payload = await client_from_instance(access.instance).find_by_name(
            resource.space_id,
            kb_id=resource.kb_id,
            filename=body.filename.strip(),
            limit=body.limit,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    return {"documents": _visible_documents(payload, resource.kb_id)}


@router.post("/documents/_list")
async def list_documents(
    body: ListDocumentsRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    access = await _member_access(
        session,
        user,
        body.agent_id,
        body.knowledge_resource_id,
    )
    resource = access.resources[0]
    try:
        payload = await client_from_instance(access.instance).list_documents(
            resource.space_id,
            kb_id=resource.kb_id,
            size=body.size,
            after_doc_id=body.after_doc_id,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    documents = _visible_document_page(payload, resource.kb_id)
    return {
        "documents": documents,
        "has_more": payload["has_more"],
        "next_after_doc_id": payload.get("next_after_doc_id"),
    }


def _visible_document_page(
    payload: dict[str, Any],
    expected_kb_id: str,
) -> list[dict[str, Any]]:
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "POLARRAG_INVALID_RESPONSE",
                "message": "PolarRAG returned an invalid document response.",
            },
        )
    allowed = (
        "doc_id",
        "kb_id",
        "filename",
        "file_size_bytes",
        "created_at",
        "status",
        "chunk_count",
        "active_generation",
        "revision_status",
    )
    documents: list[dict[str, Any]] = []
    for document in raw_documents:
        if (
            not isinstance(document, dict)
            or document.get("kb_id") != expected_kb_id
            or not isinstance(document.get("doc_id"), str)
            or not document["doc_id"]
        ):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "POLARRAG_INVALID_RESPONSE",
                    "message": "PolarRAG returned an invalid document response.",
                },
            )
        documents.append(
            {key: document[key] for key in allowed if key in document}
        )
    return documents


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: Annotated[str, Path(min_length=1, max_length=512)],
    body: DocumentResourceRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    access = await _member_access(
        session,
        user,
        body.agent_id,
        body.knowledge_resource_id,
    )
    resource = access.resources[0]
    client = client_from_instance(access.instance)
    await _authorized_document(client, resource, doc_id, access.acl_context)
    try:
        payload = await client.delete_document(
            resource.space_id,
            doc_id,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    if payload.get("doc_id") != doc_id:
        raise _upstream_error(
            PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        )
    result = {
        key: payload[key]
        for key in ("doc_id", "task_id", "status")
        if key in payload
    }
    await log_audit(
        session,
        user_id=user.id,
        action="polarrag.doc_delete",
        target_type="knowledge_resource",
        target_id=resource.id,
        status=AuditStatus.SUCCESS,
        client_info=_audit_context(
            resource,
            agent_id=body.agent_id,
            doc_id=doc_id,
        ),
        required=True,
    )
    return result


@router.post("/documents/{doc_id}/rechunk")
async def rechunk_document(
    doc_id: Annotated[str, Path(min_length=1, max_length=512)],
    body: RechunkDocumentRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    access = await _member_access(
        session,
        user,
        body.agent_id,
        body.knowledge_resource_id,
    )
    resource = access.resources[0]
    client = client_from_instance(access.instance)
    await _authorized_document(client, resource, doc_id, access.acl_context)
    strategy = None if body.chunk_strategy == "inherit" else body.chunk_strategy
    try:
        payload = await client.rechunk_document(
            resource.space_id,
            doc_id,
            chunk_strategy=strategy,
            chunk_max_tokens=body.chunk_max_tokens,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    if payload.get("doc_id") != doc_id:
        raise _upstream_error(
            PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        )
    result = {
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
    await log_audit(
        session,
        user_id=user.id,
        action="polarrag.doc_rechunk",
        target_type="knowledge_resource",
        target_id=resource.id,
        status=AuditStatus.SUCCESS,
        client_info=_audit_context(
            resource,
            agent_id=body.agent_id,
            doc_id=doc_id,
            chunk_strategy=body.chunk_strategy,
            chunk_max_tokens=body.chunk_max_tokens,
            noop=result.get("noop"),
        ),
        required=True,
    )
    return result
