from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from server.core.crypto import decrypt
from server.models import PolarRAGInstance
from server.polarrag.contracts import (
    PolarRAGCapabilities,
    PolarRAGClient,
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGOperationNotSupported,
    PolarRAGSpaceRecord,
    PolarRAGUpstreamError,
)
from server.polarrag.oss import validate_endpoint

_PLUGIN_ROOT = "/_plugins/_polar_rag"
_CATALOG_PAGE_SIZE = 100
_MAX_CATALOG_PAGES = 10_000
_RERANKER_NOT_CONFIGURED_PREFIX = (
    "reranker is not configured for space:"
)
_DOCUMENT_UPLOAD_FORBIDDEN_TYPES = frozenset(
    {
        "acl_actor_not_in_principals",
        "identity_domain_mismatch",
        "kb_actor_must_be_user",
        "kb_upload_forbidden",
    }
)
logger = logging.getLogger(__name__)


def _validate_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid PolarRAG endpoint")
    return base_url.rstrip("/")


class HttpPolarRAGClient(PolarRAGClient):
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        tls_verify: bool | ssl.SSLContext,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._auth = httpx.BasicAuth(username, password)
        self._verify = tls_verify
        self._transport = transport
        self._timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        document_read: bool = False,
        document_upload: bool = False,
        knowledge_resource_read: bool = False,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                auth=self._auth,
                verify=self._verify,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.UNAVAILABLE,
                retryable=True,
            ) from exc
        if response.status_code in {401, 403}:
            if (
                response.status_code == 403
                and document_upload
                and (error_type := self._document_upload_error_type(response))
            ):
                logger.warning(
                    "PolarRAG document upload denied: error_type=%s",
                    error_type,
                )
                raise PolarRAGUpstreamError(
                    PolarRAGErrorCode.DOCUMENT_UPLOAD_FORBIDDEN,
                    status_code=403,
                )
            code = (
                PolarRAGErrorCode.DOCUMENT_NOT_ACCESSIBLE
                if document_read and response.status_code == 403
                else PolarRAGErrorCode.AUTH_FAILED
            )
            raise PolarRAGUpstreamError(code, status_code=response.status_code)
        if response.status_code == 404:
            if document_read:
                raise PolarRAGUpstreamError(
                    PolarRAGErrorCode.DOCUMENT_NOT_ACCESSIBLE,
                    status_code=404,
                )
            if knowledge_resource_read:
                raise PolarRAGUpstreamError(
                    PolarRAGErrorCode.KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE,
                    status_code=404,
                )
        if response.status_code >= 500:
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.UNAVAILABLE,
                retryable=True,
                status_code=response.status_code,
            )
        if (
            response.status_code == 400
            and self._is_reranker_not_configured(response)
        ):
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.RERANKER_NOT_CONFIGURED,
                status_code=400,
            )
        if response.status_code >= 400:
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.INVALID_RESPONSE,
                status_code=response.status_code,
            )
        try:
            decoded = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.INVALID_RESPONSE,
                status_code=response.status_code,
            ) from exc
        if not isinstance(decoded, dict):
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.INVALID_RESPONSE,
                status_code=response.status_code,
            )
        return decoded

    @staticmethod
    def _document_upload_error_type(
        response: httpx.Response,
    ) -> str | None:
        try:
            decoded = response.json()
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(decoded, dict):
            return None
        error_type = decoded.get("error_type")
        return (
            error_type
            if error_type in _DOCUMENT_UPLOAD_FORBIDDEN_TYPES
            else None
        )

    @staticmethod
    def _is_reranker_not_configured(response: httpx.Response) -> bool:
        try:
            decoded = response.json()
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(decoded, dict):
            return False
        error = decoded.get("error")
        reasons: list[str] = []
        if isinstance(error, str):
            reasons.append(error)
        elif isinstance(error, dict):
            reason = error.get("reason")
            if isinstance(reason, str):
                reasons.append(reason)
            root_causes = error.get("root_cause")
            if isinstance(root_causes, list):
                reasons.extend(
                    item["reason"]
                    for item in root_causes
                    if isinstance(item, dict)
                    and isinstance(item.get("reason"), str)
                )
        return any(
            reason.startswith(_RERANKER_NOT_CONFIGURED_PREFIX)
            and bool(
                reason[len(_RERANKER_NOT_CONFIGURED_PREFIX) :].strip()
            )
            for reason in reasons
        )

    async def check_capabilities(self) -> PolarRAGCapabilities:
        await self._request("GET", "/")
        version_response = await self._request(
            "GET",
            f"{_PLUGIN_ROOT}/_version",
        )
        version = version_response.get("version")
        space_catalog = True
        knowledge_base_catalog = True
        spaces: list[PolarRAGSpaceRecord] = []
        try:
            spaces = await self._read_catalog(
                f"{_PLUGIN_ROOT}/spaces/_list",
                parse_item=self._parse_space,
                page_size=1,
                max_pages=1,
                allow_truncated=True,
            )
        except PolarRAGOperationNotSupported:
            space_catalog = False
            knowledge_base_catalog = False
        if space_catalog:
            try:
                if spaces:
                    await self._read_catalog(
                        (
                            f"{_PLUGIN_ROOT}/spaces/"
                            f"{quote(spaces[0].space_id, safe='')}"
                            "/knowledge_bases/_list"
                        ),
                        parse_item=lambda item: self._parse_knowledge_base(
                            item,
                            spaces[0].space_id,
                        ),
                        page_size=1,
                        max_pages=1,
                        allow_truncated=True,
                    )
                else:
                    await self._probe_knowledge_base_catalog()
            except PolarRAGOperationNotSupported:
                knowledge_base_catalog = False
        return PolarRAGCapabilities(
            version=version if isinstance(version, str) else None,
            search=True,
            protected_document_info=True,
            protected_context=True,
            protected_document_search=True,
            space_catalog=space_catalog,
            knowledge_base_catalog=knowledge_base_catalog,
        )

    async def list_spaces(self) -> list[PolarRAGSpaceRecord]:
        return await self._read_catalog(
            f"{_PLUGIN_ROOT}/spaces/_list",
            parse_item=self._parse_space,
        )

    async def list_knowledge_bases(
        self,
        space_id: str,
    ) -> list[PolarRAGKnowledgeBaseRecord]:
        if not space_id:
            raise ValueError("space_id is required")
        return await self._read_catalog(
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                "/knowledge_bases/_list"
            ),
            parse_item=lambda item: self._parse_knowledge_base(
                item,
                space_id,
                "ACTIVE",
            ),
        )

    async def list_unclaimed_knowledge_bases(
        self,
        space_id: str,
    ) -> list[PolarRAGKnowledgeBaseRecord]:
        if not space_id:
            raise ValueError("space_id is required")
        records = await self._read_catalog(
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                "/knowledge_bases/_list"
            ),
            parse_item=lambda item: self._parse_knowledge_base(
                item,
                space_id,
                "UNCLAIMED",
            ),
            statuses=("UNCLAIMED",),
        )
        if any(record.kb_type != "PERSONAL" for record in records):
            raise self._invalid_response()
        return records

    async def claim_knowledge_base(
        self,
        space_id: str,
        kb_id: str,
        *,
        owner: str,
    ) -> None:
        if not space_id or not kb_id or not owner:
            raise ValueError("space_id, kb_id, and owner are required")
        response = await self._request(
            "POST",
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                f"/knowledge_bases/{quote(kb_id, safe='')}/_claim"
            ),
            payload={"owner": owner},
        )
        if (
            response.get("space_id") != space_id
            or response.get("kb_id") != kb_id
            or response.get("status") != "ACTIVE"
            or response.get("kb_type") != "PERSONAL"
        ):
            raise self._invalid_response()

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
    ) -> dict[str, Any]:
        if not space_id or not kb_id:
            raise ValueError("space_id and kb_id are required")
        response = await self._request(
            "POST",
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                "/managed_documents"
            ),
            payload={
                "kb_id": kb_id,
                "oss_path": oss_path,
                "filename": filename,
                "file_type": file_type,
                "file_md5": file_md5,
                "file_size_bytes": file_size_bytes,
                "metadata": metadata,
                "acl_context": acl_context,
                "acl": {"mode": "POLARRAG_DERIVED"},
            },
            document_upload=True,
        )
        document = response.get("document")
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("doc_id"), str)
            or not document["doc_id"]
            or document.get("space_id") != space_id
            or document.get("kb_id") != kb_id
            or not isinstance(document.get("status"), str)
            or not document["status"]
        ):
            raise self._invalid_response()
        return document

    async def delete_document(
        self,
        space_id: str,
        doc_id: str,
        *,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        if not space_id or not doc_id:
            raise ValueError("space_id and doc_id are required")
        return await self._request(
            "DELETE",
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                f"/managed_documents/{quote(doc_id, safe='')}"
            ),
            payload={"acl_context": acl_context},
            document_read=True,
        )

    async def rechunk_document(
        self,
        space_id: str,
        doc_id: str,
        *,
        chunk_strategy: str | None,
        chunk_max_tokens: int | None,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        if not space_id or not doc_id:
            raise ValueError("space_id and doc_id are required")
        payload: dict[str, Any] = {"acl_context": acl_context}
        if chunk_strategy is not None:
            payload["chunk_strategy"] = chunk_strategy
        if chunk_max_tokens is not None:
            payload["chunk_max_tokens"] = chunk_max_tokens
        return await self._request(
            "PUT",
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                f"/documents/{quote(doc_id, safe='')}/chunk_strategy"
            ),
            payload=payload,
            document_read=True,
        )

    async def _read_catalog(
        self,
        path: str,
        *,
        parse_item: Callable[[dict[str, Any]], Any],
        page_size: int = _CATALOG_PAGE_SIZE,
        max_pages: int = _MAX_CATALOG_PAGES,
        allow_truncated: bool = False,
        statuses: tuple[str, ...] = ("ACTIVE",),
    ) -> list[Any]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        records: list[Any] = []
        for _page_number in range(max_pages):
            payload: dict[str, Any] = {
                "size": page_size,
                "statuses": list(statuses),
            }
            if cursor is not None:
                payload["cursor"] = cursor
            try:
                page = await self._request("POST", path, payload=payload)
            except PolarRAGUpstreamError as exc:
                if exc.status_code in {404, 501}:
                    raise PolarRAGOperationNotSupported from exc
                raise
            items = page.get("items")
            has_more = page.get("has_more")
            next_cursor = page.get("next_cursor")
            if (
                not isinstance(items, list)
                or not isinstance(has_more, bool)
                or (
                    next_cursor is not None
                    and (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                    )
                )
                or has_more != (next_cursor is not None)
            ):
                raise self._invalid_response()
            for item in items:
                if not isinstance(item, dict):
                    raise self._invalid_response()
                record = parse_item(item)
                record_id = self._catalog_record_id(record)
                if record_id in seen_ids:
                    raise self._invalid_response()
                seen_ids.add(record_id)
                records.append(record)
            if not has_more:
                return records
            if not items or next_cursor in seen_cursors:
                raise self._invalid_response()
            if allow_truncated:
                return records
            assert next_cursor is not None
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise self._invalid_response()

    async def _probe_knowledge_base_catalog(self) -> None:
        path = (
            f"{_PLUGIN_ROOT}/spaces/__pas_capability_probe__"
            "/knowledge_bases/_list"
        )
        try:
            await self._request(
                "POST",
                path,
                payload={"size": 1, "statuses": ["ACTIVE"]},
            )
        except PolarRAGUpstreamError as exc:
            if exc.status_code == 404:
                return
            if exc.status_code == 501:
                raise PolarRAGOperationNotSupported from exc
            raise

    @staticmethod
    def _parse_space(item: dict[str, Any]) -> PolarRAGSpaceRecord:
        space_id = HttpPolarRAGClient._required_string(item, "space_id")
        status = HttpPolarRAGClient._required_string(item, "status")
        if status != "ACTIVE":
            raise HttpPolarRAGClient._invalid_response()
        oss_endpoint = HttpPolarRAGClient._optional_string(
            item,
            "oss_endpoint",
        )
        if oss_endpoint is not None:
            try:
                oss_endpoint = validate_endpoint(oss_endpoint)
            except ValueError as exc:
                raise HttpPolarRAGClient._invalid_response() from exc
        return PolarRAGSpaceRecord(
            space_id=space_id,
            name=HttpPolarRAGClient._required_string(item, "space_name"),
            identity_domain=HttpPolarRAGClient._required_string(
                item,
                "identity_domain",
            ),
            status=status,
            oss_bucket=HttpPolarRAGClient._optional_string(
                item,
                "oss_bucket",
            ),
            oss_endpoint=oss_endpoint,
        )

    @staticmethod
    def _parse_knowledge_base(
        item: dict[str, Any],
        expected_space_id: str,
        expected_status: str = "ACTIVE",
    ) -> PolarRAGKnowledgeBaseRecord:
        space_id = HttpPolarRAGClient._required_string(item, "space_id")
        status = HttpPolarRAGClient._required_string(item, "status")
        kb_type = HttpPolarRAGClient._required_string(item, "kb_type")
        if space_id != expected_space_id or status != expected_status:
            raise HttpPolarRAGClient._invalid_response()
        raw_owner = item.get("owner")
        owner: dict[str, str] | None
        if status == "ACTIVE" and kb_type == "PERSONAL":
            if not isinstance(raw_owner, dict):
                raise HttpPolarRAGClient._invalid_response()
            owner = {
                "provider": HttpPolarRAGClient._required_string(
                    raw_owner,
                    "provider",
                ),
                "type": HttpPolarRAGClient._required_string(raw_owner, "type"),
                "id": HttpPolarRAGClient._required_string(raw_owner, "id"),
            }
            if owner["type"] != "user":
                raise HttpPolarRAGClient._invalid_response()
        else:
            if raw_owner is not None:
                raise HttpPolarRAGClient._invalid_response()
            owner = None
        updated_at_value = HttpPolarRAGClient._required_string(
            item,
            "updated_at",
        )
        try:
            updated_at = datetime.fromisoformat(
                updated_at_value.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise HttpPolarRAGClient._invalid_response() from exc
        if updated_at.tzinfo is None:
            raise HttpPolarRAGClient._invalid_response()
        return PolarRAGKnowledgeBaseRecord(
            space_id=space_id,
            kb_id=HttpPolarRAGClient._required_string(item, "kb_id"),
            name=HttpPolarRAGClient._required_string(item, "name"),
            kb_type=kb_type,
            identity_domain=HttpPolarRAGClient._required_string(
                item,
                "identity_domain",
            ),
            owner=owner,
            updated_at=updated_at,
            status=status,
        )

    @staticmethod
    def _required_string(value: dict[str, Any], field: str) -> str:
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise HttpPolarRAGClient._invalid_response()
        return field_value

    @staticmethod
    def _optional_string(value: dict[str, Any], field: str) -> str | None:
        field_value = value.get(field)
        if field_value is None:
            return None
        if not isinstance(field_value, str) or not field_value.strip():
            raise HttpPolarRAGClient._invalid_response()
        return field_value.strip()

    @staticmethod
    def _catalog_record_id(record: Any) -> str:
        if isinstance(record, PolarRAGSpaceRecord):
            return record.space_id
        if isinstance(record, PolarRAGKnowledgeBaseRecord):
            return record.kb_id
        raise HttpPolarRAGClient._invalid_response()

    @staticmethod
    def _invalid_response() -> PolarRAGUpstreamError:
        return PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)

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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query_text": query,
            "kb_id": kb_id,
            "search_mode": search_mode,
            "size": top_k,
            "reranker": reranker,
            "acl_context": acl_context,
        }
        if min_score is not None:
            payload["min_score"] = min_score
        return await self._request(
            "POST",
            f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}/search",
            payload=payload,
            knowledge_resource_read=True,
        )

    async def fetch_context(
        self,
        space_id: str,
        doc_id: str,
        *,
        chunk_index: int,
        window_size: int,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._protected_document_call(
            space_id,
            doc_id,
            "chunks/_context",
            {
                "chunk_index": chunk_index,
                "window_size": window_size,
                "acl_context": acl_context,
            },
        )

    async def find_by_name(
        self,
        space_id: str,
        *,
        kb_id: str,
        filename: str,
        limit: int,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}/managed_documents/_find",
            payload={
                "kb_id": kb_id,
                "filename": filename,
                "size": limit,
                "acl_context": acl_context,
            },
            document_read=True,
        )

    async def document_info(
        self,
        space_id: str,
        doc_id: str,
        *,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._protected_document_call(
            space_id,
            doc_id,
            "_info",
            {"acl_context": acl_context},
        )

    async def list_documents(
        self,
        space_id: str,
        *,
        kb_id: str,
        size: int,
        after_doc_id: str | None,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "size": size,
            "acl_context": acl_context,
        }
        if after_doc_id is not None:
            payload["after_doc_id"] = after_doc_id
        response = await self._request(
            "POST",
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                f"/knowledge_bases/{quote(kb_id, safe='')}"
                "/documents/_list"
            ),
            payload=payload,
            document_read=True,
        )
        documents = response.get("documents")
        has_more = response.get("has_more")
        next_after_doc_id = response.get("next_after_doc_id")
        if (
            not isinstance(documents, list)
            or not isinstance(has_more, bool)
            or (
                next_after_doc_id is not None
                and not isinstance(next_after_doc_id, str)
            )
            or (has_more and not next_after_doc_id)
        ):
            raise self._invalid_response()
        return response

    async def recall_document(
        self,
        space_id: str,
        doc_id: str,
        *,
        kb_id: str,
        query: str,
        top_k: int,
        acl_context: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._protected_document_call(
            space_id,
            doc_id,
            "chunks/_search",
            {
                "kb_id": kb_id,
                "query_text": query,
                "size": top_k,
                "acl_context": acl_context,
            },
        )

    async def _protected_document_call(
        self,
        space_id: str,
        doc_id: str,
        suffix: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            (
                f"{_PLUGIN_ROOT}/spaces/{quote(space_id, safe='')}"
                f"/managed_documents/{quote(doc_id, safe='')}/{suffix}"
            ),
            payload=payload,
            document_read=True,
        )


def client_from_instance(
    instance: PolarRAGInstance,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HttpPolarRAGClient:
    try:
        verify: bool | ssl.SSLContext = instance.tls_verify
        if instance.ca_bundle_ciphertext:
            context = ssl.create_default_context()
            context.load_verify_locations(
                cadata=decrypt(instance.ca_bundle_ciphertext)
            )
            verify = context
        username = decrypt(instance.username_ciphertext)
        password = decrypt(instance.password_ciphertext)
    except Exception:
        raise PolarRAGUpstreamError(
            PolarRAGErrorCode.CREDENTIAL_UNAVAILABLE
        ) from None
    return HttpPolarRAGClient(
        base_url=f"{instance.scheme}://{instance.host}:{instance.port}",
        username=username,
        password=password,
        tls_verify=verify,
        transport=transport,
    )
