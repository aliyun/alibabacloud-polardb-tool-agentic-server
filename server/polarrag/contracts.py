from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class PolarRAGErrorCode(str, enum.Enum):
    AUTH_FAILED = "POLARRAG_AUTH_FAILED"
    UNAVAILABLE = "POLARRAG_UNAVAILABLE"
    INVALID_RESPONSE = "POLARRAG_INVALID_RESPONSE"
    CREDENTIAL_UNAVAILABLE = "POLARRAG_CREDENTIAL_UNAVAILABLE"
    RERANKER_NOT_CONFIGURED = "RERANKER_NOT_CONFIGURED"
    DOCUMENT_UPLOAD_FORBIDDEN = "POLARRAG_DOCUMENT_UPLOAD_FORBIDDEN"
    OPERATION_NOT_SUPPORTED = "OPERATION_NOT_SUPPORTED"
    DOCUMENT_NOT_ACCESSIBLE = "DOCUMENT_NOT_ACCESSIBLE"
    KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE = (
        "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE"
    )


class PolarRAGUpstreamError(RuntimeError):
    def __init__(
        self,
        code: PolarRAGErrorCode,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class PolarRAGOperationNotSupported(PolarRAGUpstreamError):
    def __init__(self) -> None:
        super().__init__(PolarRAGErrorCode.OPERATION_NOT_SUPPORTED)


@dataclass(frozen=True)
class PolarRAGCapabilities:
    version: str | None
    search: bool
    protected_document_info: bool
    protected_context: bool
    protected_document_search: bool
    space_catalog: bool
    knowledge_base_catalog: bool

    def as_dict(self) -> dict[str, bool | str | None]:
        return {
            "version": self.version,
            "search": self.search,
            "protected_document_info": self.protected_document_info,
            "protected_context": self.protected_context,
            "protected_document_search": self.protected_document_search,
            "space_catalog": self.space_catalog,
            "knowledge_base_catalog": self.knowledge_base_catalog,
        }


@dataclass(frozen=True)
class PolarRAGSpaceRecord:
    space_id: str
    name: str
    identity_domain: str | None
    status: str
    oss_bucket: str | None = None
    oss_endpoint: str | None = None


@dataclass(frozen=True)
class PolarRAGKnowledgeBaseRecord:
    space_id: str
    kb_id: str
    name: str
    kb_type: str
    identity_domain: str
    owner: dict[str, str] | None
    usage: str | None = None
    updated_at: datetime | None = None
    status: str = "ACTIVE"


class PolarRAGClient(Protocol):
    async def check_capabilities(self) -> PolarRAGCapabilities: ...

    async def list_spaces(self) -> list[PolarRAGSpaceRecord]: ...

    async def list_knowledge_bases(
        self,
        space_id: str,
    ) -> list[PolarRAGKnowledgeBaseRecord]: ...

    async def list_unclaimed_knowledge_bases(
        self,
        space_id: str,
    ) -> list[PolarRAGKnowledgeBaseRecord]: ...

    async def claim_knowledge_base(
        self,
        space_id: str,
        kb_id: str,
        *,
        owner: str,
    ) -> None: ...

    async def submit_document(
        self,
        space_id: str,
        kb_id: str,
        *,
        oss_path: str,
        filename: str,
        file_type: str,
        file_md5: str,
        file_size_bytes: int,
        metadata: dict[str, Any],
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def delete_document(
        self,
        space_id: str,
        doc_id: str,
        *,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def rechunk_document(
        self,
        space_id: str,
        doc_id: str,
        *,
        chunk_strategy: str | None,
        chunk_max_tokens: int | None,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def search(
        self,
        space_id: str,
        kb_id: str,
        *,
        query: str,
        search_mode: str,
        top_k: int,
        min_score: float | None,
        reranker: bool,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def fetch_context(
        self,
        space_id: str,
        doc_id: str,
        *,
        chunk_index: int,
        window_size: int,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def find_by_name(
        self,
        space_id: str,
        *,
        kb_id: str,
        filename: str,
        limit: int,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def list_documents(
        self,
        space_id: str,
        *,
        kb_id: str,
        size: int,
        after_doc_id: str | None,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def document_info(
        self,
        space_id: str,
        doc_id: str,
        *,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def recall_document(
        self,
        space_id: str,
        doc_id: str,
        *,
        kb_id: str,
        query: str,
        top_k: int,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]: ...
