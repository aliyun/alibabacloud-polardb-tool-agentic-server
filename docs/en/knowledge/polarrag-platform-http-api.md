# PolarRAG platform HTTP API

**English** | [简体中文](../../zh-cn/knowledge/polarrag-platform-http-api.md)

This guide covers the PAS HTTP resource APIs for customer knowledge platforms:
knowledge and document APIs, search, the HTTP Tool gateway, and the administrator
APIs used to bind enterprise users to knowledge resources.

The examples use `https://pas.example.com`. The deployed PAS `/openapi.json` is the
machine-readable request and response contract.

## 1. Scope and authorization model

Each resource request requires a PAS user access token with the following effective
authorization context:

- `aud` is `<PAS base URL>/api/v1`;
- `scope` includes `polarrag`;
- the token identifies a PAS user and an active Agent context;
- the user still has access to that Agent when PAS receives the request.

An external application can obtain this token through RFC 8693 Token Exchange
at `/token` or `/api/v1/external-auth/token`. The administrator registers the
calling service under **External Applications**, where PAS displays the
`client_id`, one-time `client_secret`, resource `<PAS base URL>/api/v1`, scope
`polarrag`, Agent policy, and a copy-ready request. The exchange returns a PAS
access token and no refresh token. PAS does not accept a Feishu
`user_access_token` as the resource API Bearer Token unless the separate direct
compatibility mode is enabled.

An Admin token cannot replace a user token for knowledge access. Existing Dashboard
sessions, PAS MCP OAuth, `pas_agent_` tokens, and `pas_user_agent_` tokens remain
supported where their own interfaces accept them.

Authorization is always the intersection of the active user, active Agent, current
User-to-Agent grant, Agent resource scope, identity-source binding, and PolarRAG
document ACL. Client-supplied principals or ACL context never override that state.

This release does not create or delete upstream knowledge bases, accept client-defined
document ACLs, or generate an LLM answer.

## 2. Prerequisites

Before integration, an administrator must:

1. publish PAS through HTTPS;
2. configure an active Feishu or SharePoint identity source and synchronize it;
3. register PolarRAG and bind the identity source to the target Space;
4. create an Agent, grant it PolarRAG resources, and grant users or groups access;
5. configure external token trust and register an external application when
   Token Exchange is used;
6. validate Space OSS configuration when document upload is required.

## 3. Common HTTP contract

Knowledge APIs use the PAS user access token described in section 1:

```http
Authorization: Bearer <pas-user-access-token>
Accept: application/json
```

List APIs use an opaque `cursor`. Pass `next_cursor` unchanged; `null` means the
last page. `knowledge_resource_id` comes from the knowledge-base list and is the
public PAS identifier. `doc_id` comes from upload, list, detail, or search.

Currently supported knowledge and Tool endpoints:

| Capability | Method | Path |
| --- | --- | --- |
| List knowledge bases | `GET` | `/api/v1/knowledge-bases` |
| Get a knowledge base | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}` |
| Get a Space | `GET` | `/api/v1/spaces/{space_id}` |
| List a Space's knowledge bases | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases` |
| Get a knowledge base by Space and KB ID | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}` |
| List documents | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents` |
| Upload a document | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents` |
| Get or delete a document | `GET`, `DELETE` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}` |
| List document chunks | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks` |
| Get a chunk context window | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context` |
| Get original-file metadata | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/original` |
| Request document rechunking | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk` |
| Request batch document rechunking (maximum 20 documents) | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/rechunk` |
| Find documents across selected knowledge bases | `POST` | `/api/v1/documents/_find` |
| Prepare direct-to-OSS document upload | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/document-uploads` |
| Complete direct-to-OSS document upload | `POST` | `/api/v1/document-uploads/{upload_session_id}/complete` |
| List or upload documents by Space and KB ID | `GET`, `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents` |
| Get or delete a document by Space and KB ID | `GET`, `DELETE` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}` |
| List document chunks by Space and KB ID | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks` |
| Get a chunk context window by Space and KB ID | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks/{chunk_index}/context` |
| Get original-file metadata by Space and KB ID | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/original` |
| Request rechunking by Space and KB ID | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/rechunk` |
| Request batch rechunking by Space and KB ID (maximum 20 documents) | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk` |
| Prepare direct-to-OSS upload by Space and KB ID | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/document-uploads` |
| Search one knowledge base | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/search` |
| Search one knowledge base by Space and KB ID | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/search` |
| Search multiple knowledge bases | `POST` | `/api/v1/knowledge-bases/search` |
| Search multiple knowledge bases in one Space | `POST` | `/api/v1/spaces/{space_id}/search` |
| Search one document | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/search` |
| Search one document by Space and KB ID | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/search` |
| List Tools | `GET` | `/api/v1/tools` |
| Call a Tool | `POST` | `/api/v1/tools/call` |

## 4. Knowledge bases and documents

List visible knowledge bases:

```http
GET /api/v1/knowledge-bases?limit=50&cursor=<optional-cursor>
Authorization: Bearer <platform-user-token>
```

Each item includes `knowledge_resource_id`, upstream `space_id`, local
`knowledge_space_id`, display name, upstream `kb_id`, `kb_type`, `usage`, and
`upload_ready`. `space_id + kb_id` identifies one knowledge base only when PAS
resolves that upstream `space_id` in exactly one configured instance, and one Space
can contain multiple `kb_id` values. Listing a knowledge base does not imply access
to every document; PolarRAG still applies document ACLs.

PAS-managed resources use `management_mode=NATIVE`. Administrators can set
`EXTERNAL_SYNC` with
`PUT /api/polarrag/knowledge-resources/{knowledge_resource_id}/management-mode`
for resources maintained by an external synchronization task. Such resources
remain searchable, but PAS user and MCP upload, delete, and rechunk operations
return `EXTERNAL_SYNC_RESOURCE_READ_ONLY`; list responses report
`upload_ready=false`.

List a Space or its knowledge bases, or get one knowledge base by the pair:

```http
GET /api/v1/spaces/{space_id}
GET /api/v1/spaces/{space_id}/knowledge-bases?limit=50&cursor=<optional-cursor>
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}
Authorization: Bearer <platform-user-token>
```

If the PAS local catalog resolves one `space_id` to records in multiple PolarRAG
instances, these Space routes and `space_id + kb_id` selectors return HTTP 409 with
`SPACE_ID_AMBIGUOUS`; PAS never selects an arbitrary record.

List or find readable documents:

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?limit=20&cursor=<optional-cursor>
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?filename=guide.pdf&limit=20
Authorization: Bearer <platform-user-token>
```

When PolarRAG includes `source` in a document list or detail response, PAS returns
that value unchanged. It is an upstream-owned opaque value; clients must not assume
that only the currently observed `NATIVE` and `OSS` values exist.

Find by filename across one or more knowledge bases using either identifier form:

```http
POST /api/v1/documents/_find
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "filename": "guide.pdf",
  "limit": 20,
  "targets": [
    {"knowledge_resource_id": "resource-001"},
    {"space_id": "space-001", "kb_id": "kb-policy"}
  ]
}
```

Upload exactly one file as `multipart/form-data`:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${PAS_ACCESS_TOKEN}" \
  -F "file=@./guide.pdf" \
  "${PAS_BASE_URL}/api/v1/knowledge-bases/${KNOWLEDGE_RESOURCE_ID}/documents"
```

```json
{"doc_id":"doc-001","filename":"guide.pdf","status":"DISPATCHED"}
```

PAS derives the trusted upload actor from the token and server-side enterprise
identity state. Clients cannot submit ACL principals. A successful upload means the
job was accepted; poll the document detail until parsing reaches a terminal status.

Get detail, original-file metadata, or request deletion:

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks?offset=0&limit=100
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/original
DELETE /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}
Authorization: Bearer <platform-user-token>
```

Get a protected context window around one chunk, or request asynchronous rechunking:

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context?window_size=2
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"chunk_strategy":"hybrid","chunk_max_tokens":512}
```

`chunk_index` is zero-based; `window_size` is 0 through 100. Rechunking returns
HTTP 202 after PolarRAG accepts the request. Use `inherit` without
`chunk_max_tokens` to restore the Space strategy; `hybrid` and `hierarchical` may
include a positive `chunk_max_tokens` up to 100000. Both operations have equivalent
Space-and-KB routes shown in the endpoint table.

To rechunk multiple documents in the same knowledge base, submit 1 through 20
unique document IDs. PAS submits them sequentially through the same authorization
path as the single-document endpoint:

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/rechunk
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"doc_ids":["doc-a","doc-b"],"chunk_strategy":"hybrid","chunk_max_tokens":512}
```

When every request is accepted, PAS returns HTTP 202. If one or more documents
cannot be accepted, PAS continues with the remaining IDs and returns HTTP 207:

```json
{
  "accepted": [{"doc_id":"doc-a","status":"accepted"}],
  "failed": [{"doc_id":"doc-b","status_code":403,"code":"DOCUMENT_PERMISSION_DENIED","message":"..."}]
}
```

Use `POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk`
with the same body when addressing the knowledge base by Space and KB ID.

The same document operations can use a Space and KB ID pair:

```http
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents?limit=20&cursor=<optional-cursor>
POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks?offset=0&limit=100
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/original
DELETE /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}
Authorization: Bearer <platform-user-token>
```

The original endpoint returns protected metadata, not file bytes, an OSS AccessKey,
or a signed download URL. Deletion requires PolarRAG MANAGE permission and returns
HTTP 202 when accepted.

For large files, prepare a direct-to-OSS multipart upload instead of proxying bytes
through PAS:

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/document-uploads
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "filename": "guide.pdf",
  "file_size_bytes": 1024,
  "file_md5": "<32 lowercase-or-uppercase hex characters>",
  "file_sha256": "<64 lowercase-or-uppercase hex characters>",
  "content_type": "application/pdf"
}
```

The response contains an `upload_session_id` and short-lived part URLs. Upload the
parts directly, then call `POST /api/v1/document-uploads/{upload_session_id}/complete`.
PAS validates the server-side multipart state and only submits the object to
PolarRAG after every part is present. The prepare route also has the Space-and-KB
variant in the endpoint table.

## 5. Document chunks

Chunk listing is offset-paginated in ascending `chunk_index` order. `offset` is
zero-based and `limit` is 1 through 1000. The response contains `items`, `offset`,
`limit`, `total` (currently `null` because the upstream count is not reliable), and
`next_offset`; continue while `next_offset` is not `null`. PAS probes one next
chunk only after a full page, so `next_offset` does not depend on the upstream
count. Each item is a protected chunk source and always contains `image_resources`
(an empty array when the upstream chunk has no images).

## 6. Search contract

Search one knowledge base:

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"refund policy","search_mode":"balanced","top_k":10,"min_score":null,"reranker":false}
```

The same request body can identify the knowledge base by Space and KB ID:

```http
POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"refund policy","search_mode":"balanced","top_k":10,"min_score":null,"reranker":false}
```

One request can mix multiple PAS resource IDs with `space_id + kb_id` selectors:

```http
POST /api/v1/knowledge-bases/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "query": "refund policy",
  "targets": [
    {"knowledge_resource_id": "resource-001"},
    {"space_id": "space-001", "kb_id": "kb-policy"}
  ],
  "top_k": 10
}
```

Omit `targets` to exhaustively search every knowledge resource in the effective
Agent and user scope. Explicit targets may span PolarRAG instances and Spaces;
PAS groups calls, merges results, and preserves inaccessible targets as
`partial_failures`. The effective request is limited to 1,000 resources and is
never silently truncated. PolarRAG 1.0.6 and earlier use one legacy `kb_id` per
call. PolarRAG 1.0.7 and later use `kb_ids`, batched by the runtime
`_search_capabilities.max_kb_ids` value.

For multiple knowledge bases in one Space, use the shorter path. Every supplied
`knowledge_resource_ids` value must belong to the path's `space_id`; otherwise PAS
returns HTTP 422 with `KNOWLEDGE_RESOURCE_OUTSIDE_SPACE`:

```http
POST /api/v1/spaces/{space_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "query": "refund policy",
  "kb_ids": ["kb-policy", "kb-faq"],
  "knowledge_resource_ids": ["resource-001"],
  "top_k": 10
}
```

Omitting both `kb_ids` and `knowledge_resource_ids` on this Space-scoped route
searches all effective resources in that Space. Supplying an explicit empty
array is invalid.

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"refund deadline","top_k":10}
```

```http
POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"refund deadline","top_k":10}
```

```json
{
  "items": [{
    "knowledge_resource_id": "resource-001",
    "doc_id": "doc-001",
    "chunk_index": 3,
    "content": "Original retrieved chunk",
    "score": 0.87,
    "image_resources": [{"id":"document-0/pictures/1","oss_uri":"oss://bucket/picture-1.png"}],
    "metadata": {"page_numbers":[4],"headings":[],"captions":[]}
  }],
  "top_k": 10,
  "partial_failures": []
}
```

`content` is the original retrieved Chunk, not an LLM answer. Results are ordered by
the returned relevance score. The first release exposes `chunk_index`; the upstream
response has no separate stable chunk identifier, so clients must not expect one.
ACL filtering and `min_score` can make `items` shorter than `top_k`.

## 7. MCP Tools over HTTP

List the Tool catalog and call a Tool without an MCP session or JSON-RPC:

```http
GET /api/v1/tools
Authorization: Bearer <platform-user-token>
```

The catalog is authoritative and currently includes `list_knowledge_resources`,
`kb_search`, `kb_fetch_context`, `doc_list_chunks`, `doc_find_by_name`, `doc_status`, `doc_recall`,
`doc_get_original`, `doc_delete`, `doc_rechunk`, `prepare_document_upload`, and
`complete_document_upload`.

```http
POST /api/v1/tools/call
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"name":"kb_search","arguments":{"query":"refund","knowledge_resource_ids":["resource-001"],"top_k":10}}
```

The response preserves MCP `CallToolResult`, including `content` and `isError`.
Check both the HTTP status and `isError`. The HTTP gateway reuses the same Tool
registry, input schemas, authorization, rate limits, audit path, and execution code
as `/mcp`.

## 8. Enterprise user and knowledge binding

Administrator APIs configure enterprise access. Discover IDs with
`GET /api/identity-sources`,
`GET /api/identity-sources/{identity_source_id}/directory`, and
`GET /api/identity-sources/spaces`.

Prefer the atomic preview/apply workflow:

```http
POST /api/agents/{agent_id}/enterprise-access/preview
Authorization: Bearer <admin-token>
Content-Type: application/json

{"identity_source_id":"source-id","all_synced_users":false,"directory_group_ids":["group-row-id"],"pas_user_ids":["user-id"],"knowledge_space_ids":["space-id"]}
```

After administrator review, send the returned selection and `preview_hash` to
`POST /api/agents/{agent_id}/enterprise-access/apply`. A stale preview must be shown
again; do not automatically retry it.

Lower-level automation remains available under Agent `polarrag-bindings`,
`public-resources`, `user-assignments`, and `group-assignments`. These bindings only
restrict the Agent candidate scope and never replace PolarRAG document ACLs.

## 9. Errors and security

Stable gateway errors use a `detail` object with `code` and `message`; validation
errors use FastAPI's standard array. Common codes include `AUTH_REQUIRED`,
`INVALID_GRANT`, `AGENT_ACCESS_DENIED`, `KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE`,
`DOCUMENT_NOT_ACCESSIBLE`, `IDENTITY_CONTEXT_UNAVAILABLE`,
`POLARRAG_OSS_NOT_CONFIGURED`, `POLARRAG_TOOL_LIMITED`, and
`POLARRAG_UNAVAILABLE`.

Do not retry 400, 403, 404, or 422. Retry 429 only after `Retry-After`; bounded
backoff is appropriate for temporary 502/503 responses. Preserve `X-Request-ID` for
support, but never log passwords, tokens, authorization codes, PKCE verifiers,
provider tokens, OSS credentials, or enterprise document content.

Use HTTPS in production, isolate caches by user session, and rely on PAS online
authorization rather than locally decoded JWT claims. Removing a user, Agent, or
grant invalidates subsequent access even if the token has not reached its expiry.
