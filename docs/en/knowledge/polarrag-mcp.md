# PolarRAG MCP and enterprise identities

[简体中文](../../zh-cn/knowledge/polarrag-mcp.md)

PAS can manage multiple PolarRAG instances and expose ACL-protected knowledge
queries to authenticated human users. The integration uses the existing PAS
built-in user login, User Token, MCP OAuth, metadata database, audit log, and
`PAS_ENCRYPTION_KEY`.

For administrator setup of a Feishu directory source and its Space bindings,
see [Enterprise identity sources](../administration/enterprise-identity-sources.md).

## Security boundary

Only an MCP access token whose subject is `user:<pas_user_id>` can list or call
the PolarRAG tools. Machine `pas_agent_` Tokens never receive these tools and
cannot act as a human user. A user-specific `pas_user_agent_` Token resolves to
the assigned PAS user while retaining its Agent boundary on the server.

That Agent boundary is the live set of bound PolarRAG instances. Every enabled
Space on a bound instance, including a Space enabled after the Agent binding
was created, is eligible for discovery. Eligibility is not authorization: PAS
still intersects the instance set with the current user's visible resources,
enabled Space state, and server-derived target Space `acl_context`; PolarRAG
then makes the final READ decision.

Tool arguments cannot contain a user ID, provider, identity domain, principal,
`acl_context`, endpoint, credentials, or OpenSearch DSL. PAS obtains the Space
identity domain from the trusted PolarRAG catalog, loads the active server-side
principal assignments for the authenticated user, and constructs
`acl_context`. The context contains any mapped Feishu and SharePoint
principals plus exactly one canonical `polarrag/user/<User.external_id>`
principal generated or verified by the server.
PolarRAG adds its domain principal itself. Neither callers nor administrators
can supply `polarrag/domain` or privileged `polarrag/role` principals.
PolarRAG remains the final authority for document READ access.

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
  select an eligible PAS user in the same identity domain and choose
  **Assign owner and activate**. The choice can be a mapped enterprise user
  principal or an active PAS user's native `polarrag:user` principal. PAS sends
  the selected user's canonical PAS username (`User.external_id`) to PolarRAG,
  then synchronizes the active catalog. PolarRAG derives and persists the
  native owner principal for the Space identity domain.
  PUBLIC knowledge bases are active immediately and never enter this claim
  flow. Stored credentials and CA bundles are never displayed.
- Open **Users** and choose **Enterprise Identity** on a user row to bind a
  synchronized Feishu or SharePoint identity, and to add, disable, or delete
  manual Feishu, SharePoint, or native PolarRAG user/group principals. PAS
  departments and external groups remain independent.
- Open **My Instances** as the authenticated user to review accessible database
  instances and knowledge spaces. A non-administrator can choose an assigned
  Agent to open its paginated **Knowledge bases** drawer. The drawer shows each
  knowledge-base name and its opaque PAS knowledge-resource ID. Registering
  a PolarRAG instance alone does not grant user access; the Space must be
  enabled and synchronized, and the user must resolve to an enterprise
  principal or a native `polarrag:user` principal in its identity domain. A
  non-administrator can upload a local document to
  an upload-ready knowledge resource shown for the selected Agent. The browser
  submits the Agent ID, opaque resource ID, and file; PAS derives the Space,
  knowledge base, principals, and actor from the authenticated identity and
  verifies the Agent's instance binding and PUBLIC scope. PolarRAG derives the
  document ACL from the KB
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
  The table displays the document size, chunk count, and upload time when
  PolarRAG provides them.
  Administrator status does not bypass knowledge access rules.
- On an Agent detail page, use **Configure enterprise access** for normal
  Source, subject, and Space setup; the existing instance, PUBLIC-scope, user,
  and group controls remain available for advanced changes. An assigned user
  manages their own `pas_user_agent_` Token under
  **My Instances > MCP connections**. Administrators see status only and can
  force-revoke the Token without reading its plaintext. Built-in users reveal
  an existing Token by confirming their password. SSO users have no PAS
  password, so `issue` and `regenerate` return the new plaintext once in a
  `Cache-Control: no-store` response. The UI never renders it: during that
  one-time window, the user explicitly chooses **Copy Token** or **Copy JSON
  configuration**. Regenerating requires confirmation and invalidates the old
  Token immediately. Repeated deliveries are rate limited.
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

## Configure enterprise access for an Agent

Use this sequence for a normal enterprise PolarRAG rollout:

1. Register the PolarRAG instance, then enable and synchronize the target
   Space.
2. Create the Feishu or SharePoint identity source, complete verification, and
   synchronize its directory.
3. On the Agent's **PolarRAG instances** tab, bind the target instance and
   configure its shared PUBLIC resource scope.
4. Select **Configure enterprise access**.
5. Choose one Source, explicitly select **All synchronized users** or specific
   synchronized groups or PAS users, and choose one or more eligible Spaces.
   **All synchronized users** is displayed first but is never preselected.
6. Select **Preview changes** and separately review Agent-local grants, new
   global Source-Space bindings, and existing relations that will be reused.
   A new Source-Space binding is shared by other Agents and remains when this
   Agent grant is removed.
7. Confirm the preview once. The user can then sign in and create or use their
   own Agent connection under **My Instances > MCP connections**.
8. Verify the user sees only the intersection of the Agent instance and PUBLIC
   scope, Source-Space bindings, the user's current enterprise membership, and
   PolarRAG ACL.

The operation is additive and atomic. It does not remove existing grants.
Removing a user, group, or all-users Agent grant leaves global Source-Space
bindings, Agent-instance bindings, and the shared PUBLIC scope unchanged.
Fine-grained controls remain available for advanced administration.

Automation first calls
`POST /api/agents/{agent_id}/enterprise-access/preview` with trusted internal
IDs and an explicit `all_synced_users` value, then sends the returned selection
and `preview_hash` to the corresponding `/enterprise-access/apply` endpoint. A
`409` means the preview changed and must be reviewed again; it never authorizes
an automatic retry.

Successful SSO with an empty knowledge-resource list is not proof of an SSO
failure. Check Source activity and freshness, the global Source-Space binding,
the Agent subject grant, the Agent-instance binding, and PUBLIC scope as
separate gates.

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
instances and PolarRAG knowledge resources for the **My Instances** page. With
`agent_id`, the knowledge resources are narrowed to that assigned Agent's
bound instances and PUBLIC scope; the response also includes each upstream
`kb_id`. It uses the same server-side access and resource-discovery rules as
MCP and never returns endpoint credentials or trusted ACL context.

Agent-scoped user connections use these additional routes:

- administrators manage `/api/agents/{agent_id}/polarrag-bindings` and
  `/api/agents/{agent_id}/user-assignments` and may force-revoke an assignment
  Token;
- users list `/api/me/agent-connections` and use its `issue`, `reveal`,
  `regenerate`, and `revoke` Token operations for their own assignments.

The effective resource set is the intersection of the Agent's bound PolarRAG
instances, any selected PUBLIC-resource scope, and the existing user discovery
rules. The scope applies uniformly to discovery and every resource-based MCP
Tool. It never includes PERSONAL resources and never broadens user, owner, or
document ACL access. PolarRAG still makes every document READ decision. Scope
changes are loaded on each Tool call and do not require a new Token.

`POST /api/me/polarrag/documents` accepts one multipart file, an assigned
`agent_id`, and one opaque `knowledge_resource_id`, with a 100 MiB limit. It is
available only to a non-administrator who can discover that resource through
the selected Agent. PAS uploads to the validated
Space bucket, constructs the trusted `acl_context` with all mapped principals
and a user actor, then submits the resulting OSS path to PolarRAG without a
caller-assigned `doc_id` and with `acl.mode=POLARRAG_DERIVED`. PAS uses the
canonical `polarrag/user/<User.external_id>` actor for uploads; external or
native assignments establish domain membership but do not select a different
upload actor. PolarRAG verifies PUBLIC/PERSONAL KB upload policy and returns the
authoritative document ID while creating the document ACL. If PolarRAG rejects
the enterprise identity, PAS returns `POLARRAG_DOCUMENT_UPLOAD_FORBIDDEN`;
service authentication failures remain `POLARRAG_AUTH_FAILED`. Neither error
includes the upstream response body.

The member document console uses protected paginated listing, filename search,
delete, and rechunk routes under `/api/me/polarrag/documents`. Every operation
accepts an assigned `agent_id` and an opaque `knowledge_resource_id`; PAS
verifies that the document belongs to that resource and never accepts
caller-supplied identity or ACL fields. Successful mutations are recorded in
the existing PolarRAG audit-log category.

Space enablement accepts only `space_id`. The Space name and immutable
`identity_domain` must come from the trusted upstream enumeration response;
administrators cannot type or override them.
The Spaces drawer shows whether a Space is enabled, its last synchronization
time, and a paginated synchronized knowledge-base catalog, including unresolved
PERSONAL owners.

## Enterprise principals

Administrators maintain assignments with:

- `POST /api/polarrag/users/{user_id}/principals`;
- `GET /api/polarrag/users/{user_id}/principals`;
- `PATCH /api/polarrag/users/{user_id}/principals/{principal_id}`;
- `DELETE /api/polarrag/users/{user_id}/principals/{principal_id}`.

The external providers `feishu` and `sharepoint` support `user` and `group`.
One external user principal can map to only one PAS user in an identity domain;
an external group can be assigned to multiple PAS users. Disabled or expired
assignments are excluded from `acl_context`.

For an enterprise without either external identity source, an administrator may
instead add `polarrag` as the sole mapping for the target Space identity domain.
This provider accepts only `user`, and its principal ID is locked to the
selected PAS user's `User.external_id`. The API rejects a different ID or a
`group`, `domain`, or `role` form. PAS also verifies the stored native mapping
against the current user on every discovery and Tool request and fails closed
if they differ.
If any active native assignment is malformed, PAS rejects the whole identity
domain even when a valid external assignment also exists. This avoids partially
accepting an ambiguous server-side identity state.

An external assignment still causes PAS to add the same canonical native user
in memory. A native-only assignment persists that user solely to establish
membership in the target identity domain. PolarRAG generates
`polarrag/domain/<identity_domain>` during normalization, and
`polarrag/role/*` remains system-only. Neither mapping mode bypasses PERSONAL
owner rules or expands any PolarRAG document ACL.

A PERSONAL owner remains discoverable only while that user has an active
same-domain assignment. The supported claim and catalog-sync flows already
require such an assignment before recording the owner, so existing owners need
no data migration; removing or expiring every assignment makes discovery and
Tool execution fail closed consistently.

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
- `complete_document_upload(upload_session_id)`.

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
only the session ID and file fingerprints for resume. The client calls
`complete_document_upload` with only that session ID. PAS lists multipart parts
with its server-owned OSS access and verifies their numbers, count, and sizes.
If parts are missing, the result remains `prepared` and contains fresh URLs only
for those parts. The script uploads them without repeating existing parts, then
calls `complete_document_upload` again. When every part exists, PAS completes the
multipart object, rebuilds
the current trusted actor context, and submits the object to PolarRAG. It returns only PolarRAG's
authoritative `doc_id`; PAS never assigns one. If PolarRAG submission fails
after OSS completion, a caller's bounded retry calls only PolarRAG again and does not
repeat multipart completion. Expired or abandoned sessions are reclaimed by the
server cleanup worker. Files remain
limited to 100 MiB. PAS also keeps a cleanup journal that survives user, Agent,
Space, or resource deletion. Each entry retains its creation-time OSS
coordinates and an encrypted credential snapshot so later Space configuration
or credential rotation cannot strand the old object. Foreground completion and
background cleanup use the same committed lease, random owner token, and
fencing check. The short claim transaction finishes before any OSS or PolarRAG
request, so SQLite, MySQL, and PostgreSQL do not retain a database write
transaction during remote I/O. A stale worker cannot finalize a newer claim.
Before submission, PAS also stores an encrypted request snapshot in the journal.
If a process exits or the result of a PolarRAG request is unknown, the background
worker can repeat the same idempotent submission without the original user,
Agent, or resource row. A confirmed acceptance completes local state without
deleting the OSS object. A definite upstream rejection releases the object for
abort or cleanup; timeouts and retryable failures never do. The snapshot is
deleted with the journal after successful submit, abort, or cleanup and is never
returned by an API or written to logs. The worker uses bounded retry with
backoff for temporary PolarRAG or OSS failures.

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
