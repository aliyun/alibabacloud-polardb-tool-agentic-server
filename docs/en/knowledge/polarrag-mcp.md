# PolarRAG MCP and enterprise identities

[简体中文](../../zh-cn/knowledge/polarrag-mcp.md)

PAS can manage multiple PolarRAG instances and expose ACL-protected knowledge
queries to authenticated human users. The integration uses the existing PAS
built-in user login, User Token, MCP OAuth, metadata database, audit log, and
`PAS_ENCRYPTION_KEY`.

## Security boundary

Only an MCP access token whose subject is `user:<pas_user_id>` can list or call
the PolarRAG tools. Machine `pas_agent_` Tokens never receive these tools and
cannot act as a human user. A user-specific `pas_user_agent_` Token resolves to
the assigned PAS user while retaining its Agent boundary on the server.

Tool arguments cannot contain a user ID, provider, identity domain, principal,
`acl_context`, endpoint, credentials, or OpenSearch DSL. PAS obtains the Space
identity domain from the trusted PolarRAG catalog, loads the active server-side
principal assignments for the authenticated user, and constructs
`acl_context`. PolarRAG remains the final authority for document READ access.

PAS resource discovery only determines which knowledge bases a user may select:

- an active `PUBLIC` knowledge base uses the `DOMAIN` discovery rule;
- an active `PERSONAL` knowledge base uses the `OWNER` discovery rule;
- unknown knowledge-base types and unresolved owners fail closed.

Discovery never grants document access and PAS does not query PolarRAG metadata
tables or system indexes.

## Web console

PolarRAG administration is integrated into the existing PAS pages:

- Open **Instances**, choose **Register Instance**, and select `PolarRAG` from
  the Engine field to register an endpoint and store its shared OpenSearch
  account and TLS settings. The **PolarRAG Instances** tab runs capability
  checks, rotates write-only credentials, disables an instance, and enables or
  synchronizes or disables an enumerated Space. If an enabled Space contains an
  `UNCLAIMED` PERSONAL knowledge base, its Spaces drawer lets an administrator
  select an eligible PAS user principal in the same identity domain and choose
  **Assign owner and activate**. PAS builds the trusted actor context, asks
  PolarRAG to claim the knowledge base, and synchronizes the active catalog.
  PUBLIC knowledge bases are active immediately and never enter this claim
  flow. Stored credentials and CA bundles are never displayed.
- Open **Users** and choose **Enterprise Identity** on a user row to maintain
  that user's allowlisted Feishu or SharePoint user/group principals. PAS
  departments and external groups remain independent.
- Open **My Instances** as the authenticated user to review accessible database
  instances and PolarRAG knowledge resources. Registering a PolarRAG instance
  alone does not grant user access; the Space must be enabled and synchronized,
  and the user must have a valid principal assignment for its identity domain.
  A non-administrator can upload a local document to an upload-ready knowledge
  resource shown on this page. The browser submits only the opaque resource ID
  and file; PAS derives the Space, knowledge base, principals, and actor from
  the authenticated identity. PolarRAG derives the document ACL from the KB
  policy and trusted actor. The **Manage documents** action finds documents
  by filename and lets the user request deletion or rechunking. PAS forwards
  the same trusted identity context, and PolarRAG enforces `MANAGE` for delete
  and `EXECUTE` for rechunk. Administrators cannot upload or manage documents.
  If **Upload** is disabled, its tooltip directs the user to ask an
  administrator to configure and validate the Space OSS AccessKey credentials.
  After PolarRAG accepts an upload, PAS leaves the user on **My Instances** with
  the acceptance result. Opening **Manage documents** loads one ACL-filtered
  page of 20 documents and their current statuses. **Previous**, **Next**, and
  **Refresh** are explicit user actions; PAS does not poll in the background.
  The table also displays the chunk count when PolarRAG provides it.
  An administrator with no accessible knowledge resources sees a control-plane
  access explanation and a link to the PolarRAG management tab; administrator
  status does not bypass knowledge access rules.
- On an Agent detail page, bind its allowed PolarRAG instances and assign PAS
  users. An assigned user manages their own `pas_user_agent_` Token under
  **My Instances > MCP connections**. Administrators see status only and can
  force-revoke the Token without reading its plaintext.
- For administrators, Dashboard **Instances** and **Active** totals include both
  registered database instances and registered PolarRAG instances. Pool
  availability remains database-only. Members instead see only the counts of
  database instances and PolarRAG knowledge resources accessible to their own
  account; global administration statistics and actions are hidden.

The legacy `/polarrag` URL redirects to
`/instances?type=polarrag`; there is no separate sidebar entry.

The page shows `POLARRAG_CATALOG_CAPABILITY_MISSING` when the upstream plugin
cannot provide the trusted Space and knowledge-base catalog. Administrators
cannot type an identity domain or bypass this capability check.

## Register an instance

Administrator routes are under `/api/polarrag`:

- `POST /instances` registers and checks one endpoint;
- `GET /instances` and `GET /instances/{id}` return redacted configuration;
- `PATCH /instances/{id}` rotates the shared account, TLS settings, or name;
- `POST /instances/{id}/check` repeats authentication and capability checks;
- `DELETE /instances/{id}` disables the instance and its Spaces;
- `GET /instances/{id}/spaces` enumerates trusted upstream Spaces and includes
  each Space's synchronized knowledge resources, opaque resource IDs, binding
  modes, and synchronization status;
- `POST /instances/{id}/spaces/enable` enables one enumerated Space and
  immediately synchronizes its active knowledge bases;
- `DELETE /instances/{id}/spaces/{space_id}` disables one Space and
  immediately hides its knowledge resources;
- `POST /instances/{id}/spaces/{space_id}/sync` runs an explicit catalog sync.
- `PUT /spaces/{knowledge_space_id}/oss-config` validates and saves the OSS
  AccessKey pair and object prefix for an enabled Space. The bucket and endpoint
  are read-only and come from the trusted PolarRAG Space catalog.
- `GET /instances/{id}/unclaimed-knowledge-bases` lists `UNCLAIMED` knowledge
  bases in enabled Spaces and eligible owner principals;
- `POST /instances/{id}/spaces/{space_id}/knowledge-bases/{kb_id}/claim`
  assigns an eligible same-domain user principal as owner, activates the
  upstream knowledge base, synchronizes the Space, and records an audit event.

The create request contains `name`, `scheme`, `host`, `port`, `username`,
`password`, `tls_verify`, and optional `ca_bundle`. PAS encrypts the username,
password, and CA bundle with the existing root key. No response returns these
values. PAS also synchronizes enabled Spaces every five minutes.

The Space OSS configuration reuses `PAS_ENCRYPTION_KEY` for AccessKey storage.
Saving it uses the catalog endpoint to write and delete a zero-byte probe in the
catalog-provided bucket, so the supplied credentials must allow both operations.
PAS never returns the AccessKey pair. A changed upstream bucket or endpoint
immediately invalidates the saved configuration and requires revalidation.

`GET /api/me/resources` returns the authenticated user's accessible database
instances and PolarRAG knowledge resources for the **My Instances** page. It
uses the same server-side access and resource-discovery rules as MCP and never
returns endpoint credentials or trusted ACL context.

Agent-scoped user connections use these additional routes:

- administrators manage `/api/agents/{agent_id}/polarrag-bindings` and
  `/api/agents/{agent_id}/user-assignments` and may force-revoke an assignment
  Token;
- users list `/api/me/agent-connections` and use its `issue`, `reveal`,
  `regenerate`, and `revoke` Token operations for their own assignments.

The effective resource set is the intersection of the Agent's bound PolarRAG
instances and the existing user discovery rules. PolarRAG still makes every
document READ decision.

`POST /api/me/polarrag/documents` accepts one multipart file and one opaque
`knowledge_resource_id`, with a 100 MiB limit. It is available only to a
non-administrator who can discover that resource. PAS uploads to the validated
Space bucket, constructs the trusted `acl_context` with all mapped principals
and a user actor, then submits the resulting OSS path to PolarRAG without a
caller-assigned `doc_id` and with `acl.mode=POLARRAG_DERIVED`. PolarRAG verifies
PUBLIC/PERSONAL KB upload policy and returns the authoritative document ID while
creating the document ACL. If PolarRAG rejects the submission, PAS attempts to
delete the staged object and returns a sanitized error.

The member document console uses protected paginated listing, filename search,
delete, and rechunk routes under `/api/me/polarrag/documents`. Every operation
accepts an opaque `knowledge_resource_id`; PAS verifies that the document
belongs to that resource and never accepts caller-supplied identity or ACL
fields. Successful mutations are recorded in the existing PolarRAG audit-log
category.

Space enablement accepts only `space_id`. The Space name and immutable
`identity_domain` must come from the trusted upstream enumeration response;
administrators cannot type or override them.
The Spaces drawer shows whether a Space is enabled, its last synchronization
time, and the synchronized knowledge-base catalog, including unresolved
PERSONAL owners.

## Enterprise principals

Administrators maintain assignments with:

- `POST /api/polarrag/users/{user_id}/principals`;
- `GET /api/polarrag/users/{user_id}/principals`;
- `PATCH /api/polarrag/users/{user_id}/principals/{principal_id}`;
- `DELETE /api/polarrag/users/{user_id}/principals/{principal_id}`.

The allowed providers are `feishu` and `sharepoint`; the principal type is
`user` or `group`. One external user principal can map to only one PAS user in
an identity domain. An external group can be assigned to multiple PAS users.
Disabled or expired assignments are excluded from `acl_context`.

PAS departments continue to provide local group membership. They are
independent from external enterprise groups; phase one does not create a
mapping between them. Remote identity-provider adapters are defined as an
interface only and no Feishu or SharePoint remote calls or batch imports run in
phase one.

## MCP tools

The user-only catalog contains:

- `list_knowledge_resources(cursor?, limit?)`;
- `kb_search(query, knowledge_resource_ids, search_mode?, top_k?,
  min_score?, reranker?)`;
- `kb_fetch_context(knowledge_resource_id, doc_id, chunk_index,
  window_size?)`;
- `doc_find_by_name(knowledge_resource_ids, filename, limit?)`;
- `doc_status(knowledge_resource_id, doc_id)`;
- `doc_recall(knowledge_resource_id, doc_id, query, top_k?)`;
- `doc_get_original(knowledge_resource_id, doc_id)`;
- `doc_delete(knowledge_resource_id, doc_id)`;
- `doc_rechunk(knowledge_resource_id, doc_id, chunk_strategy?,
  chunk_max_tokens?)`.

The user-specific Agent Token catalog additionally contains:

- `prepare_document_upload(knowledge_resource_id, filename, file_size_bytes,
  file_md5, file_sha256, content_type?)`;
- `resume_document_upload(upload_session_id)`;
- `complete_document_upload(upload_session_id, parts)`;
- `abort_document_upload(upload_session_id)`.

`knowledge_resource_ids` is mandatory where present. Accessible resources in
one request must belong to one PolarRAG instance and one Space. `kb_search` can
query multiple knowledge bases concurrently, merge hits by score, and return
sanitized `partial_failures` for an inaccessible knowledge resource or a
retryable per-KB upstream failure. Mixed instances, mixed Spaces, invalid
arguments, invalid authentication, or an unavailable identity context fail the
whole request.

When `reranker=true` but the selected Space has no reranker model configured,
`kb_search` fails with `RERANKER_NOT_CONFIGURED` and a safe explanatory
message. The response is not retryable until an administrator configures the
Space; a client may explicitly retry with `reranker=false` only when non-reranked
retrieval is acceptable. PAS does not expose the upstream response body, Space
identifier, request identifier, account, endpoint, or credential details.

`doc_get_original` calls the protected PolarRAG document-info route. After
PolarRAG authorizes READ, PAS returns only file metadata such as `filename`,
`file_type`, size, content hash, and `oss_path`. PAS does not download or proxy
the file, generate a signed URL, or handle OSS credentials.

`doc_delete` is destructive and calls PolarRAG only after it has authorized
`MANAGE`. `doc_rechunk` requires PolarRAG `EXECUTE`; `chunk_strategy` can be
`hybrid`, `hierarchical`, or `inherit`. `inherit` restores the current Space
default and can return `noop`. Both tools first verify the document against the
selected knowledge resource and use only the server-derived ACL context.

The upload tools never accept a local path, file bytes, OSS coordinates,
credentials, identity, or ACL fields. `prepare_document_upload` creates a
24-hour upload session and returns 8 MiB multipart URLs that expire after 15
minutes. An approved local script streams the file directly to OSS and saves
only the session ID, file fingerprints, and completed part ETags for resume.
`resume_document_upload` reads OSS state and returns fresh URLs only for missing
parts. `complete_document_upload` verifies every reported ETag and part size
against OSS before completing the object, rebuilds the current trusted actor
context, and submits the object to PolarRAG. It returns only PolarRAG's
authoritative `doc_id`; PAS never assigns one. If PolarRAG submission fails
after OSS completion, a caller's bounded retry calls only PolarRAG again and does not
repeat multipart completion. `abort_document_upload` can clean up only an
unfinished session owned by the current PAS user and Agent. Files remain
limited to 100 MiB.

## Required PolarRAG capabilities

PAS requires the protected search, document-info, filename-find, context, and
within-document search routes. Document management additionally uses:

- `POST /_plugins/_polar_rag/spaces/{space_id}/managed_documents`;
- `DELETE /_plugins/_polar_rag/spaces/{space_id}/managed_documents/{doc_id}`;
- `PUT /_plugins/_polar_rag/spaces/{space_id}/documents/{doc_id}/chunk_strategy`.

PAS also probes the fixed trusted catalog routes:

- `POST /_plugins/_polar_rag/spaces/_list`;
- `POST /_plugins/_polar_rag/spaces/{space_id}/knowledge_bases/_list`.

The Space catalog must return immutable non-empty `identity_domain` values and
the non-secret `oss_bucket` for upload-enabled Spaces.
The knowledge-base catalog must return active KB metadata and a structured
`owner` for every `PERSONAL` KB. Both catalogs use opaque cursor pagination.

The protected document-submit route accepts server-derived Space/KB
coordinates, OSS object metadata, trusted principals and actor, and the
`POLARRAG_DERIVED` ACL marker. The configured shared OpenSearch account does not
need to be `admin` or the metastore user; PolarRAG authorizes the upload from
the trusted actor and derives the ACL from the active KB.

An incompatible instance is recorded as `capability_missing`; Space
enumeration or enablement returns `OPERATION_NOT_SUPPORTED`. PAS does not
accept manually supplied catalog facts as a fallback.
