# PolarRAG platform HTTP API

**English** | [简体中文](../../zh-cn/knowledge/polarrag-platform-http-api.md)

This guide covers the PAS HTTP resource APIs for customer knowledge platforms:
knowledge and document APIs, search, the HTTP Tool gateway, and the administrator
APIs used to bind enterprise users to knowledge resources.

The examples use `https://pas.example.com`. Export the machine-readable request and
response contract offline from the matching PAS version using
`python -m server.openapi --surface application`. Online `/openapi.json` is disabled.

## 1. Scope and authorization model

### Administrator catalog synchronization

`POST /api/v1/admin/spaces/{space_id}/sync` synchronizes a registered Space's
catalog. PAS resolves its instance; do not supply `instance_id`. This endpoint
and its matching `GET` status endpoint require an active PAS administrator's
console login access JWT (`Authorization: Bearer <login-access-token>`) or
`session_token` cookie. Ordinary users receive 403; missing/invalid credentials
receive 401. Platform OAuth, external token exchange, personal and Agent Tokens
cannot replace administrator login credentials here. Knowledge must be enabled.

Omit the body or send `{}` for existing full synchronization. On PolarRAG versions
newer than 1.0.7, send `{"kb_ids":["pkb-a","pkb-b"]}` for targeted synchronization: 1–100 string IDs,
at most 256 characters each. IDs are trimmed and deduplicated. Empty arrays,
blank IDs, null and incorrect types receive 422. PAS forwards `kb_ids` on every
upstream `knowledge_bases/_list` page, requiring PolarRAG support for that filter;
there is no local full-catalog filtering or incomplete single-KB metadata fallback.
For registered plugin versions 1.0.7 or older (also missing/unrecognized versions),
PAS ignores validated `kb_ids` and performs full synchronization without sending
`kb_ids` upstream. This includes full-sync missing-KB retirement, Space-level
failure handling and full result counts; do not rely on target isolation on these
versions. Both plain semantic versions and PolarRAG package version strings are supported.

HTTP 202 with `status: running` means accepted, not completed. Poll
`GET /api/v1/admin/spaces/{space_id}/sync` for `completed` or `failed`. This is
the latest Space-level state, not retained per-request history. Completion and
catalog updates commit atomically. Unregistered Spaces return 404; disabled
Spaces and an active sync lease return 409. Full and targeted sync share the lease.
If a Space ID is registered under multiple instances, both new endpoints return
409 with `AMBIGUOUS_SPACE`, including when one registration is disabled. PAS does
not choose an instance or start a sync; use the existing instance-qualified
endpoint to address the intended registration.

When targeted synchronization is supported, all targeted IDs must appear in the ACTIVE upstream catalog. Missing, inactive
or wrong-Space targets produce `failed` with `POLARRAG_CATALOG_TARGETS_MISSING`;
no target updates commit. Unexpected or malformed results fail with
`POLARRAG_INVALID_RESPONSE`. Other upstream errors also fail. Targeted failures
roll back all KB changes, including upstream HTTP 409. Non-target KBs, Agent
bindings and user grants are untouched. Result counts describe requested KBs
only; targeted sync neither retires missing KBs nor updates the last full-sync
timestamp. Metadata registration does not automatically grant access.

Existing `POST` and `GET`
`/api/polarrag/instances/{instance_id}/spaces/{space_id}/sync` remain compatible,
including the legacy POST's 202 response when synchronization is already running.

### User resource authorization

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

Personal Tokens use `/mcp/personal` and are not credentials for the `/api/v1`
endpoints on this page. Personal connections access database instances within the
current account's effective permissions, including direct grants, system-assigned
instances the user owns, and department/user-group inheritance. They also
search/read PERSONAL knowledge bases owned by the current user.
PUBLIC knowledge bases and other users' PERSONAL knowledge bases are excluded.
PUBLIC knowledge bases require access through an authorized Agent.

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
public PAS identifier. Document lists also accept a one-based `page`; use either
`cursor` or `page`, not both. `doc_id` comes from upload, list, detail, or search.

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
| Upload documents to multiple knowledge bases in one Space (maximum 20 files) | `POST` | `/api/v1/spaces/{space_id}/documents/batch` |
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
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?limit=20&page=2
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?filename=guide.pdf&limit=20
Authorization: Bearer <platform-user-token>
```

With PolarRAG 1.0.7 or later, PAS requests pages sorted by `updated_at` in
descending order and returns exact `total`, `page`, and `size` values. It still
returns an opaque `next_cursor` while more pages exist, so existing cursor-based
clients continue without having to understand the upstream page number. Keep
the same `limit` while following that cursor; PAS rejects a different value so
page boundaries cannot repeat or skip documents. Earlier PolarRAG versions keep
the legacy document-ID cursor response and omit `total`, `page`, and `size`.
If the PolarRAG version changes while a client is paging, PAS rejects a cursor
from the other pagination protocol; restart from the first page.

When PolarRAG includes `source` in a document list or detail response, PAS returns
that value unchanged. It is an upstream-owned opaque value; clients must not assume
that only the currently observed `NATIVE` and `OSS` values exist.

For PolarRAG 1.0.7 or later, document list, filename lookup, and document detail
responses also include an `acl` object. PAS reads permissions in batches of at most
200 document IDs and exposes only the validated ACL fields returned for the current
caller:

```json
{
  "acl": {
    "identity_domain": "tenant-a",
    "state": "ACTIVE",
    "updating": false,
    "acl_version": 3,
    "acl_set_id": "base64url-acl-set-id",
    "grants": [
      {
        "principal": {"provider": "feishu", "type": "group", "id": "engineering"},
        "actions": ["READ"]
      }
    ]
  }
}
```

A caller with `MANAGE` may receive the full grants, while a read-only caller receives
only the caller's effective grant, as enforced by PolarRAG. `acl: null` means that
the newer plugin returned no active ACL for that readable document. Earlier plugin
versions omit `acl` and PAS does not call the permission endpoint.

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

Each filename lookup `items` entry includes `knowledge_resource_id`, `space_id`,
and `kb_id` alongside the existing `knowledge_space_id` and nested `document`.
The pair comes from the PAS resource catalog, even when the upstream document
omits those fields. Partial failures follow the identifier rules in section 6.

Upload exactly one file as `multipart/form-data`:

The URL selects the knowledge base; only the `file` field is required in the multipart body.

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

To upload up to 20 files to different knowledge bases in the same Space, repeat
the `files` and `kb_ids` multipart fields in matching order:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${PAS_ACCESS_TOKEN}" \
  -F "files=@./policy.pdf" \
  -F "files=@./faq.md" \
  -F "kb_ids=kb-policy" \
  -F "kb_ids=kb-faq" \
  "${PAS_BASE_URL}/api/v1/spaces/${SPACE_ID}/documents/batch"
```

PAS validates every file and target knowledge base before uploading anything.
All accepted files return HTTP 201. A definitive per-file OSS or PolarRAG
failure affects only its corresponding file and returns HTTP 207 with ordered
`index` values:

```json
{
  "accepted": [{
    "index": 0,
    "knowledge_resource_id": "resource-policy",
    "kb_id": "kb-policy",
    "doc_id": "doc-001",
    "filename": "policy.pdf",
    "status": "DISPATCHED"
  }],
  "failed": [{
    "index": 1,
    "knowledge_resource_id": "resource-faq",
    "kb_id": "kb-faq",
    "filename": "faq.md",
    "status_code": 502,
    "code": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
    "message": "PolarRAG did not accept the uploaded document."
  }]
}
```

The two multipart lists must have the same length. Authorization, Space
membership, knowledge scope, `EXTERNAL_SYNC` write protection, OSS readiness,
filename rules, and the 100 MiB per-file limit are checked before the first OSS
write. PAS uploads the objects to OSS and submits all successfully uploaded
objects through one PolarRAG `managed_documents/_bulk` request. It does not send
`doc_id`; PolarRAG generates each document ID. The operation is not transactional
after uploads begin; retry only the entries reported in `failed`.
If PolarRAG does not return a definitive bulk result, PAS returns HTTP 502 with
`POLARRAG_BATCH_SUBMIT_RESULT_UNKNOWN` and preserves the uploaded OSS objects.
Do not retry that request blindly because PolarRAG may already have accepted it.

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

Search responses preserve `doc_generation`. To keep a context window consistent
with a selected search hit, pass its generation as the optional
`expected_doc_generation` query parameter (integer 0 through 2147483647):

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context?window_size=2&expected_doc_generation=2
```

Both context routes also accept an optional URL-encoded `query` containing the
original query. On HTTP 409 `citation_source_changed`, PAS uses that query to
re-locate a passage within the same document and KB once, then reads the fresh
Chunk with its new index and generation pin. Missing hits or generation and
repeated conflicts return HTTP 409 with `detail.code=citation_source_changed`.
The conflict also covers an invisible or missing published generation and a
missing selected Chunk. Discard old hits and assembled context; never retry
without the pin. When `query` is omitted, the caller must repeat its original
search with the original query and selectors at most once before fetching a
fresh pinned hit. If automatic recovery with `query` has failed, stop instead of
starting another retry loop. This recovery does not re-run a cross-KB search;
ACL errors retain their existing meanings. Existing unpinned requests remain
supported, without a search-to-context consistency guarantee.

Chunk list and context responses include the authorized document's original
`filename` on every Chunk entry. PAS obtains it from the ACL-checked document
record; it does not accept a client-supplied filename or issue one lookup per Chunk.
Names are returned unchanged, including Unicode, spaces, and multiple suffixes.
The upstream Chunk `source_uri` is passed through when present:

```json
{
  "items": [{
    "doc_id": "doc-001",
    "filename": "退款 指南.backup.v2.pdf",
    "source_uri": "oss://bucket/refund.pdf",
    "chunk_index": 3,
    "text": "Original Chunk"
  }],
  "offset": 0,
  "limit": 100,
  "total": 1,
  "next_offset": null
}
```

### Force rechunk

Single-document and batch rechunk requests accept optional `force` (default
`false`). `force=true` requires PolarRAG 1.0.7 or later; an older or unknown
version returns HTTP 422 with `POLARRAG_VERSION_UNSUPPORTED` for a single document.
For a batch, such failures appear in `failed` with `status_code: 422`; the overall
response is HTTP 207, including when every document fails. Force rechunk retains
user, Agent, KB, and document authorization checks.

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"chunk_strategy":"hybrid","chunk_max_tokens":512,"force":true}
```

Use `POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/rechunk`
with the same body when addressing the knowledge base by Space and KB ID.

### Batch rechunk

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
`limit`, `total`, and `next_offset`; continue while `next_offset` is not `null`.
When PolarRAG returns an exact hit count (`relation=eq`), PAS exposes it as
`total` and calculates `next_offset` from that count. Otherwise `total` is `null`
and PAS probes one next chunk only after a full page. Each item is a protected
chunk source and always contains `image_resources` (an empty array when the
upstream chunk has no images).

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
never silently truncated. For multiple KBs, PAS probes the runtime
`_search_capabilities` endpoint and batches `kb_ids` by `max_kb_ids`; the stored
plugin version does not override advertised capabilities. When the capability
endpoint returns 400, 404, or 501, or advertises `multi_kb_search: false`, PAS uses one legacy `kb_id` per call.
Upstream search HTTP 429 is treated as temporary unavailability: successful KB
results remain available and affected KBs appear in `partial_failures` as
`POLARRAG_UNAVAILABLE`. If no KB succeeds, the request returns HTTP 503.

Every knowledge-base and single-document search response includes the original
`filename` and upstream `source_uri` from the ACL-authorized hit in each `items`
entry. A missing or null upstream `source_uri` is returned as `null`. The
responses from `POST /api/v1/knowledge-bases/search` and
`POST /api/v1/spaces/{space_id}/search` additionally include `space_id` and
`kb_id` alongside `knowledge_resource_id`. Each pair belongs to that resource,
including when results span Spaces with the same KB ID.
`partial_failures` also includes both fields for accessible resources. For
`KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE`, both fields are `null` to avoid disclosing
resource identifiers. A resource no longer present in the PAS catalog also has
`null` pair fields.

These are additive HTTP response fields: existing IDs, content, status fields,
pagination, sorting, errors, and request selectors retain their meanings.
Clients should ignore unknown response fields. Native MCP Tool results and
`/api/v1/tools/call` expose the same filename from their existing authorized
search hit or document lookup. Search tools also return the upstream `source_uri`.

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
    "filename": "退款 指南.backup.v2.pdf",
    "source_uri": "oss://bucket/refund.pdf",
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

Administrators can manage all Agent members with
`GET /api/agents/{agent_id}/all-subject-assignments` and `POST` to the same path.
POST accepts `{"operation":"BIND"}` or `{"operation":"UNBIND"}`. BIND adds all
usable identity sources with an enabled Space in a bound instance, and all active
built-in users, independent of pagination. Source-wide grants include future
synchronized users; new built-in users or sources require another BIND.
UNBIND removes all user, department, group, and source memberships from this Agent
and revokes their user Agent Tokens. Group-assignment-specific KB grants are
removed; KB user policies, other Agents, and database-instance grants remain.
Both operations are idempotent. GET and POST return `status` (`BOUND`, `PARTIAL`,
`UNBOUND`), `eligible_source_count`, `eligible_user_count`, and `assignment_count`.
Missing Agents return `404`, non-admin callers `403`, missing authentication
`401`, invalid requests `422`, and a running legacy bulk assignment job `409`.
This manages Agent membership, not KB-level all-user policies.

For Agent-to-KB visibility, `GET /api/admin/agents/{agent_id}/knowledge-resource-options`
accepts optional `binding_status=bound|unbound`, `management_mode`, `search`, `offset`
and `limit` (1–100). Filtering occurs before pagination and the response `total`.
`POST /api/admin/agents/{agent_id}/knowledge-visibility:bind` and `:unbind` accept
`{"knowledge_resource_ids":["<resource-id>"]}` or
`{"targets":[{"space_id":"<space-id>","kb_id":"<kb-id>"}]}` (1–100 entries,
exactly one selector field) for atomic, idempotent
native PUBLIC/PERSONAL KB selection. Targets must be active, enabled and within
bound instances and eligible Spaces; invalid batches return `422`. Both return
`instance_count`, per-type `statuses`, `agent_id`, and the sorted IDs accepted in
`knowledge_resource_ids`. User policies and Agent membership are
preserved; PERSONAL KBs remain owner-only. Unbinding part of an all-KB range
retains the dynamic default and records KB exclusions; future native and synced
KBs remain eligible. Rebinding removes the selected exclusions; either all-type
action clears that type's exclusions. Explicit allowlists remain explicit during
migration. The console separates Bound/Unbound pages with multi-selection.

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
## Space/KB selector compatibility

Internal resource IDs remain supported. The following equivalent forms accept both `space_id` and `kb_id`; a standalone `kb_id` is not sufficient:

| Operation | Coordinate form |
| --- | --- |
| `POST /api/admin/agents/{agent_id}/knowledge-visibility:bind` and `:unbind` | `{"targets":[{"space_id":"space-1","kb_id":"kb-1"}]}` instead of `knowledge_resource_ids` |
| `PUT /api/agents/{agent_id}/polarrag-bindings/{binding_id}/public-resources` | `{"targets":[{"space_id":"space-1","kb_id":"kb-1"}]}` instead of `public_knowledge_resource_ids` |
| `POST /api/admin/agents/{agent_id}/knowledge-binding-status`, `/knowledge-subject-options`, `/knowledge-all-users-status`, and `/knowledge-all-users` | `{"targets":[{"space_id":"space-1","kb_id":"kb-1"}]}` instead of `knowledge_resource_ids`; retain other request fields |
| `PUT /api/polarrag/knowledge-resources/{knowledge_resource_id}/management-mode` | `PUT /api/polarrag/spaces/{space_id}/knowledge-bases/{kb_id}/management-mode`, with the same body |
| `/api/me/polarrag/documents/_find`, `/_list`, `/{doc_id}` (DELETE), `/{doc_id}/rechunk` | JSON `space_id` and `kb_id` instead of `knowledge_resource_id`; retain `agent_id` and other operation fields |
| `POST /api/me/polarrag/documents` | Multipart `space_id` and `kb_id` when `knowledge_resource_id` is omitted; retain `agent_id` and `file` |
| `POST /api/me/polarrag/documents/batch` | Multipart `space_id` and repeated `kb_ids` instead of repeated `knowledge_resource_ids`; each `kb_ids` entry matches the corresponding `files` entry |

PUBLIC scope PUT replaces the entire selection: `targets: []` clears it and `targets: null` restores dynamic all-PUBLIC selection. Exactly one of `targets` and `public_knowledge_resource_ids` must be present. Validation and audit use the existing replacement operation; this differs from incremental `knowledge-visibility:bind`.

The four administrator request bodies above require exactly one nonempty selector list (`targets` or `knowledge_resource_ids`, 1–100 entries). Batch document upload requires either `knowledge_resource_ids` or `space_id` with `kb_ids`, never both; it checks every file against the user's Agent and KB access before uploading.

Document JSON and multipart selectors are mutually exclusive: mixing an internal ID with either coordinate returns 422 before uploading. Pair resolution does not alter Agent/user authorization, resource management rules or document ACLs. Missing pairs return 404; ambiguous pairs across eligible instances return 409. Management-mode aliases use the same administrator check, update and required audit as the internal-ID route.

Existing batch knowledge bindings, external identity-source binding snapshots, `/api/v1/documents/_find` and `/api/v1/knowledge-bases/search` already accept `targets` entries with `space_id` and `kb_id`. Existing `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}` document aliases are unchanged. Space search accepts `kb_ids` under the Space path; omitting all selectors uses the caller's visible Agent scope.


## Enterprise user selectors and identity responses

Administrator authentication remains required. An enterprise identity pair is a
user selector, not an authentication credential. Existing PAS IDs and all old
endpoints remain supported; pagination, ordering, names and encoded `external_id`
keep their original meaning.

For `USER` knowledge subjects, provide either `user_id` or the complete pair:

```json
{"type":"USER","identity_source_id":"source-id","external_user_id":"enterprise-subject"}
```

Agent user assignment uses the same selector without `type`. Half a pair or both
selectors returns 422. Unknown sources or current mappings return 404 without
creating, synchronizing or allocating a user. Other subject types retain their
original field combinations. In a batch, old and enterprise selectors for the
same account are deduplicated before the existing binding transaction. The source
in an external scope path describes that scope; each enterprise USER must still
specify its own identity source.

Enterprise-access preview/apply accepts `external_user_ids` under the existing
`identity_source_id`, up to 500 IDs of 1–255 characters each. `pas_user_ids` still
defaults to `[]`; explicitly passing this empty array with enterprise IDs is
supported. Both arrays nonempty returns 422. PAS resolves the current mapping
before the existing service calculates its unchanged preview hash. A mapping
change can invalidate the preview: refresh preview after a stale 409 before apply.
Response `selection` retains `pas_user_ids` and adds `external_user_ids` for the
selected source.

User-assignment items, user options, knowledge binding USER subjects and knowledge
subject options add `enterprise_identities`. Each element has only
`identity_source_id` and `external_user_id`. All current identities are returned
in deterministic order, with `[]` for unmapped or deleted users. Explicit mapping
ownership takes precedence over legacy canonical identity fallback; an identity
reassigned to another account is not returned to the old account. Scope responses
retain `user_ids` and add `enterprise_users`, each containing `user_id` and
`enterprise_identities`. Agent create/list/get/patch retain `created_by` and add
`created_by_enterprise_identities`; this is output information, not an input that
can override the authenticated creator.

The four identity-mapping compatibility endpoints are:

| Method | Path |
| --- | --- |
| GET | `/api/identity-sources/enterprise-users/identities` |
| POST | `/api/identity-sources/enterprise-users/identities` |
| PUT | `/api/identity-sources/enterprise-users/identities/{identity_id}` |
| DELETE | `/api/identity-sources/enterprise-users/identities/{identity_id}` |

All require query `identity_source_id` and `external_user_id` to select the target
PAS account. GET retains `offset`, `limit` and `search`. POST/PUT body fields
`identity_source_id` and `external_user_id` describe the identity being bound or
replaced, which can belong to a different source from the target query. Encode
query values normally, including subjects containing `/` or `:`. Existing synced
identity edit/unbind restrictions, conflict checks, default-account restoration
and audit semantics remain in force. The four old `/users/{user_id}/identities`
operations continue to work.

The following existing operations support the selectors or additive identity
responses described above:

| Method | Path |
| --- | --- |
| POST | `/api/agents/{agent_id}/enterprise-access/preview` |
| POST | `/api/agents/{agent_id}/enterprise-access/apply` |
| GET | `/api/admin/agents/{agent_id}/knowledge-bindings` |
| POST | `/api/admin/agents/{agent_id}/knowledge-binding-status` |
| POST | `/api/admin/agents/{agent_id}/knowledge-subject-options` |
| POST | `/api/admin/agents/{agent_id}/knowledge-bindings:batch` |
| PUT | `/api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}` |
| GET | `/api/agents/{agent_id}/user-assignments` |
| POST | `/api/agents/{agent_id}/user-assignments` |
| GET | `/api/agents/{agent_id}/user-options` |
| POST | `/api/agents` |
| GET | `/api/agents` |
| GET | `/api/agents/{agent_id}` |
| PATCH | `/api/agents/{agent_id}` |
| GET | `/api/identity-sources/users/{user_id}/identities` |
| POST | `/api/identity-sources/users/{user_id}/identities` |
| PUT | `/api/identity-sources/users/{user_id}/identities/{identity_id}` |
| DELETE | `/api/identity-sources/users/{user_id}/identities/{identity_id}` |
