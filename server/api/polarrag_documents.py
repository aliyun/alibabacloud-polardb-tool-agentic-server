from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Response,
    UploadFile,
)
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
from server.polarrag.client import client_from_instance, supports_optimized_document_apis
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
from server.polarrag.upload_cleanup import is_definitive_rejection
from server.polarrag.write_policy import require_pas_managed_resource

router = APIRouter(prefix="/me/polarrag", tags=["polarrag-documents"])
MAX_BATCH_UPLOAD_FILES = 20
logger = logging.getLogger(__name__)


class DocumentResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: str = Field(min_length=1, max_length=36)
    knowledge_resource_id: str | None = Field(default=None, min_length=1, max_length=512)
    space_id: str | None = Field(default=None, min_length=1, max_length=255)
    kb_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_selector(self) -> DocumentResourceRequest:
        if self.knowledge_resource_id is not None:
            if self.space_id is not None or self.kb_id is not None:
                raise ValueError("knowledge_resource_id cannot be combined with space_id or kb_id")
        elif self.space_id is None or self.kb_id is None:
            raise ValueError("provide knowledge_resource_id or both space_id and kb_id")
        return self


class FindDocumentsRequest(DocumentResourceRequest):
    filename: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=20, ge=1, le=1000)


class ListDocumentsRequest(DocumentResourceRequest):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=200)
    after_doc_id: str | None = Field(default=None, min_length=1, max_length=512)


class RechunkDocumentRequest(DocumentResourceRequest):
    chunk_strategy: Literal["inherit", "hybrid", "hierarchical"] = "inherit"
    chunk_max_tokens: int | None = Field(default=None, ge=1, le=100_000)
    force: bool = False

    @model_validator(mode="after")
    def validate_inherited_strategy(self) -> RechunkDocumentRequest:
        if self.chunk_strategy == "inherit" and self.chunk_max_tokens is not None:
            raise ValueError("chunk_max_tokens requires an explicit chunk_strategy")
        return self


async def _member_access(
    session: AsyncSession,
    user: User,
    agent_id: str,
    knowledge_resource_id: str | None,
    space_id: str | None = None,
    kb_id: str | None = None,
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
            raise KnowledgeAccessError(KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE)
        resource_scope = await resolve_polarrag_resource_scope_for_agent(session, agent_id, user.id)
        if knowledge_resource_id is None:
            from server.polarrag.resource_selector import resolve_knowledge_pair
            if not space_id or not kb_id:
                raise HTTPException(422, "Provide both space_id and kb_id")
            knowledge_resource_id = await resolve_knowledge_pair(
                session, space_id, kb_id, instance_ids=list(resource_scope.bindings),
            )
        elif space_id is not None or kb_id is not None:
            raise HTTPException(422, "knowledge_resource_id cannot be combined with space_id or kb_id")
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


async def _member_batch_access(
    session: AsyncSession,
    user: User,
    agent_id: str,
    knowledge_resource_ids: list[str] | None,
    space_id: str | None,
    kb_ids: list[str] | None,
) -> tuple[KnowledgeAccessPlan, list[str]]:
    if user.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_DOCUMENT_UPLOAD_FORBIDDEN",
                "message": "Administrators cannot upload PolarRAG documents.",
            },
        )
    try:
        agent = await session.get(Agent, agent_id)
        if (
            agent is None
            or agent.status != AgentStatus.ACTIVE
            or not await has_agent_access(session, agent_id, user.id)
        ):
            raise KnowledgeAccessError(KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE)
        resource_scope = await resolve_polarrag_resource_scope_for_agent(session, agent_id, user.id)
        if kb_ids is not None:
            from server.polarrag.resource_selector import resolve_knowledge_pair

            knowledge_resource_ids = [
                await resolve_knowledge_pair(session, space_id or "", kb_id, instance_ids=list(resource_scope.bindings))
                for kb_id in kb_ids
            ]
        assert knowledge_resource_ids is not None
        access = await plan_knowledge_access(
            session,
            user,
            list(dict.fromkeys(knowledge_resource_ids)),
            resource_scope=resource_scope,
        )
        if access.partial_failures:
            raise KnowledgeAccessError(KnowledgeAccessErrorCode.NO_ACCESSIBLE_RESOURCE)
        return access, knowledge_resource_ids
    except KnowledgeAccessError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
                "message": "Knowledge resource is not accessible.",
            },
        ) from None


def _upstream_error(exc: PolarRAGUpstreamError) -> HTTPException:
    if exc.code == PolarRAGErrorCode.EXTERNAL_SYNC_RESOURCE_READ_ONLY:
        return HTTPException(
            status_code=409,
            detail={
                "code": exc.code.value,
                "message": "Externally synchronized knowledge resources are read-only in PAS.",
            },
        )
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
        "source",
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


def _visible_document_acl(
    value: Any,
    *,
    expected_doc_id: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("doc_id") != expected_doc_id:
        raise _invalid_document_response()
    identity_domain = value.get("identity_domain")
    state = value.get("state")
    updating = value.get("updating")
    acl_version = value.get("acl_version")
    acl_set_id = value.get("acl_set_id")
    grants = value.get("grants")
    if (
        not isinstance(identity_domain, str)
        or not identity_domain
        or not isinstance(state, str)
        or not state
        or not isinstance(updating, bool)
        or not isinstance(acl_version, int)
        or isinstance(acl_version, bool)
        or acl_version < 0
        or not isinstance(acl_set_id, str)
        or not acl_set_id
        or not isinstance(grants, list)
    ):
        raise _invalid_document_response()
    visible_grants: list[dict[str, Any]] = []
    for grant in grants:
        principal = grant.get("principal") if isinstance(grant, dict) else None
        actions = grant.get("actions") if isinstance(grant, dict) else None
        if (
            not isinstance(principal, dict)
            or not all(
                isinstance(principal.get(key), str) and principal[key]
                for key in ("provider", "type", "id")
            )
            or not isinstance(actions, list)
            or any(
                not isinstance(action, str)
                or action not in {"READ", "WRITE", "MANAGE"}
                for action in actions
            )
            or len(set(actions)) != len(actions)
        ):
            raise _invalid_document_response()
        visible_grants.append(
            {
                "principal": {
                    key: principal[key] for key in ("provider", "type", "id")
                },
                "actions": list(actions),
            }
        )
    return {
        "identity_domain": identity_domain,
        "state": state,
        "updating": updating,
        "acl_version": acl_version,
        "acl_set_id": acl_set_id,
        "grants": visible_grants,
    }


def _invalid_document_response() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "code": "POLARRAG_INVALID_RESPONSE",
            "message": "PolarRAG returned an invalid document response.",
        },
    )


async def attach_document_permissions(
    client: PolarRAGClient,
    space_id: str,
    documents: list[dict[str, Any]],
    acl_context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not documents or not bool(getattr(client, "supports_plugin_107", False)):
        return documents
    permission_context = dict(acl_context)
    raw_permissions: list[Any] = []
    try:
        permission_context["actor"] = document_actor(acl_context)
        doc_ids = [document["doc_id"] for document in documents]
        for offset in range(0, len(doc_ids), 200):
            payload = await client.list_document_permissions(
                space_id,
                doc_ids[offset : offset + 200],
                acl_context=permission_context,
            )
            batch_permissions = payload.get("documents")
            if not isinstance(batch_permissions, list):
                raise _invalid_document_response()
            raw_permissions.extend(batch_permissions)
    except (ValueError, PolarRAGUpstreamError) as exc:
        if isinstance(exc, PolarRAGUpstreamError):
            raise _upstream_error(exc) from exc
        raise _invalid_document_response() from exc
    requested_ids = {document["doc_id"] for document in documents}
    permissions: dict[str, dict[str, Any]] = {}
    for raw_permission in raw_permissions:
        doc_id = raw_permission.get("doc_id") if isinstance(raw_permission, dict) else None
        if not isinstance(doc_id, str) or doc_id not in requested_ids or doc_id in permissions:
            raise _invalid_document_response()
        permissions[doc_id] = _visible_document_acl(
            raw_permission,
            expected_doc_id=doc_id,
        )
    return [
        {**document, "acl": permissions.get(document["doc_id"])}
        for document in documents
    ]


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


def _require_upload_ready(access: KnowledgeAccessPlan) -> None:
    for resource in access.resources:
        try:
            require_pas_managed_resource(resource)
        except PolarRAGUpstreamError as exc:
            raise _upstream_error(exc) from exc
    space = access.space
    if not space.oss_config_validated or not space.oss_bucket or not space.oss_object_prefix:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_OSS_NOT_CONFIGURED",
                "message": "The Space OSS upload configuration is not ready.",
            },
        )


async def _validate_upload_file(
    file: UploadFile,
) -> tuple[str, str, str, str, int]:
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
    return filename, file_type, file_md5, file_sha256, file_size


def _upload_actor(access: KnowledgeAccessPlan) -> dict[str, str]:
    try:
        return cast(dict[str, str], document_actor(access.acl_context))
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDENTITY_CONTEXT_UNAVAILABLE",
                "message": "The trusted enterprise identity is unavailable.",
            },
        ) from None


def _upload_store(access: KnowledgeAccessPlan):
    try:
        return object_store_from_space(access.space)
    except Exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_OSS_NOT_CONFIGURED",
                "message": "The Space OSS upload configuration is not ready.",
            },
        ) from None


async def _cleanup_batch_upload_object(
    store,
    key: str,
    *,
    index: int,
    knowledge_resource_id: str,
) -> None:
    try:
        await store.delete(key)
    except Exception:
        logger.exception(
            "polarrag.batch_upload.oss_cleanup_failed index=%d knowledge_resource_id=%s",
            index,
            knowledge_resource_id,
        )


async def _upload_validated_document(
    *,
    access: KnowledgeAccessPlan,
    resource: KnowledgeResource,
    file: UploadFile,
    validated: tuple[str, str, str, str, int],
    actor: dict[str, str],
    store,
    user: User,
    agent_id: str,
    session: AsyncSession,
) -> dict[str, Any]:
    filename, file_type, file_md5, file_sha256, file_size = validated
    acl_context = dict(access.acl_context)
    acl_context["actor"] = actor
    key = document_object_key(
        cast(str, access.space.oss_object_prefix),
        new_upload_object_id(),
        filename,
    )
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
            access.space.space_id,
            resource.kb_id,
            oss_path=f"oss://{access.space.oss_bucket}/{key}",
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


@router.post("/documents", status_code=201)
async def upload_document(
    agent_id: str = Form(...),
    knowledge_resource_id: Annotated[str | None, Form(min_length=1, max_length=512)] = None,
    space_id: Annotated[str | None, Form(min_length=1, max_length=255)] = None,
    kb_id: Annotated[str | None, Form(min_length=1, max_length=255)] = None,
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
        session,
        user,
        agent_id,
        knowledge_resource_id,
        space_id,
        kb_id,
    )
    _require_upload_ready(access)
    validated = await _validate_upload_file(file)
    return await _upload_validated_document(
        access=access,
        resource=access.resources[0],
        file=file,
        validated=validated,
        actor=_upload_actor(access),
        store=_upload_store(access),
        user=user,
        agent_id=agent_id,
        session=session,
    )


@router.post(
    "/documents/batch",
    status_code=201,
    responses={207: {"description": "Partial success"}},
)
async def upload_documents_batch(
    response: Response,
    agent_id: Annotated[str, Form()],
    files: Annotated[
        list[UploadFile], File(min_length=1, max_length=MAX_BATCH_UPLOAD_FILES)
    ],
    knowledge_resource_ids: Annotated[
        list[str] | None, Form(min_length=1, max_length=MAX_BATCH_UPLOAD_FILES)
    ] = None,
    space_id: Annotated[str | None, Form(min_length=1, max_length=255)] = None,
    kb_ids: Annotated[list[str] | None, Form(min_length=1, max_length=MAX_BATCH_UPLOAD_FILES)] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if (knowledge_resource_ids is None) == (kb_ids is None) or (kb_ids is None) != (space_id is None):
        raise HTTPException(status_code=422, detail="Provide either knowledge_resource_ids or space_id and kb_ids")
    if len(files) != len(knowledge_resource_ids or kb_ids or []):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BATCH_UPLOAD_LENGTH_MISMATCH",
                "message": "Each upload file must have one knowledge resource.",
            },
        )
    if len(files) > MAX_BATCH_UPLOAD_FILES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TOO_MANY_UPLOAD_FILES",
                "message": "A batch cannot contain more than 20 files.",
            },
        )
    access, knowledge_resource_ids = await _member_batch_access(
        session,
        user,
        agent_id,
        knowledge_resource_ids,
        space_id,
        kb_ids,
    )
    _require_upload_ready(access)
    by_id = {resource.id: resource for resource in access.resources}
    validated = [await _validate_upload_file(file) for file in files]
    actor = _upload_actor(access)
    store = _upload_store(access)
    accepted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    uploaded: list[
        tuple[
            int,
            str,
            KnowledgeResource,
            tuple[str, str, str, str, int],
            str,
        ]
    ] = []
    for index, (resource_id, resource_file, file_metadata) in enumerate(
        zip(knowledge_resource_ids, files, validated, strict=True)
    ):
        resource = by_id[resource_id]
        filename, file_type, file_md5, file_sha256, file_size = file_metadata
        key = document_object_key(
            cast(str, access.space.oss_object_prefix),
            new_upload_object_id(),
            filename,
        )
        try:
            await store.put(key, resource_file.file)
        except Exception:
            await _cleanup_batch_upload_object(
                store,
                key,
                index=index,
                knowledge_resource_id=resource_id,
            )
            failed.append(
                {
                    "index": index,
                    "knowledge_resource_id": resource_id,
                    "kb_id": resource.kb_id,
                    "filename": filename,
                    "status_code": 502,
                    "code": "OSS_UPLOAD_FAILED",
                    "message": "The document could not be uploaded to OSS.",
                }
            )
        else:
            uploaded.append((index, resource_id, resource, file_metadata, key))

    bulk_results: list[dict[str, Any]] = []
    if uploaded:
        acl_context = dict(access.acl_context)
        acl_context["actor"] = actor
        documents = [
            {
                "kb_id": resource.kb_id,
                "oss_path": f"oss://{access.space.oss_bucket}/{key}",
                "filename": file_metadata[0],
                "file_type": file_metadata[1],
                "file_md5": file_metadata[2],
                "file_size_bytes": file_metadata[4],
                "metadata": {"sha256": file_metadata[3]},
            }
            for _index, _resource_id, resource, file_metadata, key in uploaded
        ]
        try:
            bulk_results = await client_from_instance(access.instance).submit_documents(
                access.space.space_id,
                documents,
                acl_context=acl_context,
            )
        except PolarRAGUpstreamError as exc:
            if not is_definitive_rejection(exc):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "POLARRAG_BATCH_SUBMIT_RESULT_UNKNOWN",
                        "message": "PolarRAG did not return a definitive batch result.",
                    },
                ) from None
            bulk_results = [{"success": False} for _item in uploaded]
        except Exception:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "POLARRAG_BATCH_SUBMIT_RESULT_UNKNOWN",
                    "message": "PolarRAG did not return a definitive batch result.",
                },
            ) from None

    for (
        index,
        resource_id,
        resource,
        file_metadata,
        key,
    ), result in zip(uploaded, bulk_results, strict=True):
        filename, _file_type, _file_md5, _file_sha256, file_size = file_metadata
        if not result["success"]:
            await _cleanup_batch_upload_object(
                store,
                key,
                index=index,
                knowledge_resource_id=resource_id,
            )
            failed.append(
                {
                    "index": index,
                    "knowledge_resource_id": resource_id,
                    "kb_id": resource.kb_id,
                    "filename": filename,
                    "status_code": 502,
                    "code": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
                    "message": "PolarRAG did not accept the uploaded document.",
                }
            )
            continue
        document = result["document"]
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
        accepted.append(
            {
                "index": index,
                "knowledge_resource_id": resource_id,
                "kb_id": resource.kb_id,
                "doc_id": document["doc_id"],
                "filename": filename,
                "status": document.get("status", "DISPATCHED"),
            }
        )
    if failed:
        response.status_code = 207
        failed.sort(key=lambda item: item["index"])
    return {"accepted": accepted, "failed": failed}


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
        body.space_id,
        body.kb_id,
    )
    resource = access.resources[0]
    client = client_from_instance(access.instance)
    try:
        payload = await client.find_by_name(
            resource.space_id,
            kb_id=resource.kb_id,
            filename=body.filename.strip(),
            limit=body.limit,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    documents = _visible_documents(payload, resource.kb_id)
    return {
        "documents": await attach_document_permissions(
            client,
            resource.space_id,
            documents,
            access.acl_context,
        )
    }


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
        body.space_id,
        body.kb_id,
    )
    resource = access.resources[0]
    try:
        client = client_from_instance(access.instance)
        payload = await client.list_documents(
            resource.space_id,
            kb_id=resource.kb_id,
            size=body.size,
            after_doc_id=body.after_doc_id,
            acl_context=access.acl_context,
            page=body.page,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    documents = await attach_document_permissions(
        client,
        resource.space_id,
        _visible_document_page(payload, resource.kb_id),
        access.acl_context,
    )
    response = {
        "documents": documents,
        "has_more": payload["has_more"],
        "next_after_doc_id": payload.get("next_after_doc_id"),
    }
    if "total" in payload:
        response.update(
            total=payload["total"],
            page=payload["page"],
            size=payload["size"],
        )
    return response


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
        "source",
        "file_size_bytes",
        "created_at",
        "updated_at",
        "completed_at",
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
        documents.append({key: document[key] for key in allowed if key in document})
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
        body.space_id,
        body.kb_id,
    )
    resource = access.resources[0]
    try:
        require_pas_managed_resource(resource)
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
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
        raise _upstream_error(PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE))
    result = {key: payload[key] for key in ("doc_id", "task_id", "status") if key in payload}
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
        body.space_id,
        body.kb_id,
    )
    resource = access.resources[0]
    if body.force and not supports_optimized_document_apis(access.instance.plugin_version):
        raise HTTPException(
            status_code=422,
            detail={"code": "POLARRAG_VERSION_UNSUPPORTED", "message": "Force rechunk requires PolarRAG 1.0.7 or later."},
        )
    try:
        require_pas_managed_resource(resource)
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    client = client_from_instance(access.instance)
    await _authorized_document(client, resource, doc_id, access.acl_context)
    strategy = None if body.chunk_strategy == "inherit" else body.chunk_strategy
    try:
        payload = await client.rechunk_document(
            resource.space_id,
            doc_id,
            chunk_strategy=strategy,
            chunk_max_tokens=body.chunk_max_tokens,
            force=body.force,
            acl_context=access.acl_context,
        )
    except PolarRAGUpstreamError as exc:
        raise _upstream_error(exc) from exc
    if payload.get("doc_id") != doc_id:
        raise _upstream_error(PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE))
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
            force=body.force,
            noop=result.get("noop"),
        ),
        required=True,
    )
    return result
