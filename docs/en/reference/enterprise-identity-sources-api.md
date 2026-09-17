# Enterprise identity source administrator API

[简体中文](../../zh-cn/reference/enterprise-identity-sources-api.md)

This page describes administrator automation APIs for enterprise identity sources. Prefer the console workflow in [Enterprise identity sources](../administration/enterprise-identity-sources.md); use these APIs from scripts, operations platforms, or integrations.

Every endpoint is under `/api` and requires an administrator Session Cookie with `X-PAS-CSRF: 1`, or a supported administrator Bearer Token. Secret fields are write-only and no response returns a plaintext secret.

## Identity sources

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| Create source | `POST /api/identity-sources` | `name`, `provider`, optional `stale_after_seconds`; Feishu uses `app_id`, `app_secret`; SharePoint uses `tenant_id`, `cloud`, `client_id`, `client_secret` | Creates a pending-verification or pending-binding source. `provider` is `feishu` or `sharepoint`. |
| Start Feishu tenant verification | `POST /api/identity-sources/{source_id}/feishu-verification` | Path parameter `source_id` | Returns a one-time `authorization_url` for the administrator to open in a browser. Available only to pending Feishu sources. |
| List sources | `GET /api/identity-sources` | `offset` (default `0`), `limit` (default `20`, maximum `100`), optional `search` | Returns `{items, total, offset, limit}`. Search matches the source name before pagination. Each item includes source state, bound Spaces, latest sync time, sanitized `last_error`, and a separate structured `sync_warning` when a Feishu membership refresh was partial. |
| Sync now | `POST /api/identity-sources/{source_id}/sync` | Path parameter `source_id` | Starts or joins the serialized refresh for a ready Feishu or SharePoint source. Returns `202` with `last_error=SYNC_IN_PROGRESS` when the request budget expires or a run is already active; the background run continues. |
| Update source | `PUT /api/identity-sources/{source_id}` | Path parameter `source_id`; `name` and complete current-provider credentials. Feishu uses `app_id`, `app_secret`; SharePoint uses `cloud`, `client_id`, `client_secret` | Updates credentials and clears the latest sync state. Feishu must verify the tenant again; SharePoint retains its tenant ID and syncs again after binding. |
| Delete source | `DELETE /api/identity-sources/{source_id}` | Path parameter `source_id` | Deletes the source, directory snapshot, Space bindings, and source-derived grants. Existing PAS users remain. |
| Inspect synchronized directory | `GET /api/identity-sources/{source_id}/directory` | Path parameter `source_id`; `entry_type=users|groups|all`, `offset`, `limit` (default `20`, maximum `100`), optional `search` | Pages synchronized users, groups, or both for operations checks and user-identity selection. Search matches stable external IDs and display fields before pagination. |

`source_id` is the identity-source UUID returned by creation or listing. A failed sync exposes only a sanitized error type. A partial-membership warning contains only `code`, `skipped_count`, and provider-code counts; it never contains provider response bodies or credentials. Inspect protected PAS logs without recording or transmitting secrets.

Directory responses contain `users`, `groups`, and `total`. With
`entry_type=users` or `groups`, `total` is the filtered total for that type.
With `entry_type=all`, the same `offset` and `limit` are applied independently
to both arrays and `total` is `null`; use a type-specific request for a complete
page sequence and exact total. The console uses these backend pages and never
loads the complete tenant directory into one response.

## Space bindings

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| List bindable Spaces | `GET /api/identity-sources/spaces` | `offset` (default `0`), `limit` (default `20`, maximum `100`), optional `search` | Returns `{items, total, offset, limit}` for enabled PolarRAG Spaces. Search matches Space ID, name, or identity domain before pagination. |
| Bind Space | `POST /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | Path parameters `source_id`, `knowledge_space_id` | Makes synchronized principals eligible for ACL-context construction in that Space. Repeated calls are idempotent. |
| Unbind Space | `DELETE /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | Path parameters `source_id`, `knowledge_space_id` | Immediately stops contributing source principals to that Space without deleting the source or synchronized users. |

A binding makes principals ACL candidates; it does not grant knowledge-base or document access. PolarRAG ACL remains the final decision.

## Agent enterprise access orchestration

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| Preview Agent enterprise access | `POST /api/agents/{agent_id}/enterprise-access/preview` | `identity_source_id`, explicit `all_synced_users`, `directory_group_ids`, `pas_user_ids`, optional `polarrag_instance_ids`, `knowledge_space_ids` | Performs read-only authoritative validation and returns normalized `selection`, Agent-local `creates`, global Source-Space `global_changes`, existing `reuses`, and `preview_hash`. |
| Apply reviewed access | `POST /api/agents/{agent_id}/enterprise-access/apply` | The exact returned selection plus `preview_hash` | Atomically and idempotently creates missing Agent-instance bindings, Source-Space bindings, and Agent subject grants, then records required audit entries. Any validation, write, commit, or audit failure rolls back the complete operation. |

Requests contain only PAS internal source, directory-row, user, and Space IDs.
They must not contain `provider`, `principal_id`, `identity_domain`, credentials,
or `acl_context`; PAS derives these trusted values from current server-side
records. `all_synced_users` defaults to `false`, but clients should always send
the current explicit choice. At least one subject and one enabled Space are
required. Clients should send a non-empty `polarrag_instance_ids` list; for
compatibility with v0.0.9 requests that omit the field, PAS derives the sorted
instance IDs from the selected enabled Spaces, validates that every derived
instance is active, and returns those IDs in the normalized `selection`.
Explicit instance IDs must still be non-empty, and every selected instance must
contribute at least one selected enabled Space.
Each request accepts at most 500 `directory_group_ids`, 500 `pas_user_ids`, and
200 `polarrag_instance_ids` and `knowledge_space_ids`.

Apply revalidates current Source freshness, directory state, instance and Space
state, Agent-instance bindings, and the previewed relationships. If anything changed,
it writes nothing and returns `409` with
`detail.code=ENTERPRISE_ACCESS_PREVIEW_STALE` and a refreshed preview. The
client must show that preview and obtain a new confirmation; it must not
automatically apply it. New Source-Space bindings are global shared state.
Removing a resulting Agent user, group, or all-users grant later does not
remove those global bindings, Agent-instance bindings, or shared PUBLIC scope.

## Agent knowledge scopes

| Operation | Method and path | Effect |
| --- | --- | --- |
| List bindings | `GET /api/admin/agents/{agent_id}/knowledge-bindings` | Returns one paginated row per bound subject, including origin, subject identity, resolved Space/KB identifiers, and the Agent scope mode. Supports `search`, `origin`, and `subject_type`. |
| List resource options | `GET /api/admin/agents/{agent_id}/knowledge-resource-options` | Returns a searchable page of active knowledge resources inside the Agent-wide PolarRAG ceiling. |
| Batch manual bindings | `POST /api/admin/agents/{agent_id}/knowledge-bindings:batch` | Applies up to 100 ordered `BIND` or `UNBIND` operations atomically; `activate_scoped_mode=true` switches the Agent to `SCOPED`. |
| Change mode | `PUT /api/admin/agents/{agent_id}/knowledge-scope-mode` | Sets `mode` to `LEGACY_ALL` or `SCOPED`. |
| Replace external bindings | `PUT /api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}` | Idempotently replaces one source-owned full binding snapshot. |
| Delete external bindings | `DELETE /api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}` | Deletes one source-owned binding snapshot. |

Each operation or external snapshot contains `subjects` and `targets`. A
subject is exactly one of `{"type":"USER","user_id":"..."}`,
`{"type":"DEPARTMENT","department_id":"..."}`, or
`{"type":"GROUP","identity_source_id":"...","group_id":"..."}`. A target
contains either `knowledge_resource_id` or both `space_id` and `kb_id`. PAS
deduplicates subjects and targets before validating them. One batch accepts at
most 5,000 distinct subjects. One operation or external snapshot accepts at
most `max_exhaustive_knowledge_resources` distinct targets.

`external_scope_id` is the source system's stable scope identifier, not a PAS
scope ID or PolarRAG KB ID. For Feishu Wiki synchronization it is the Feishu
Wiki `space_id`. The idempotency key is `(agent_id, identity_source_id,
external_scope_id)`. The producer must raise the configured limit instead of
splitting one external scope; PAS never automatically shards it.

Matching direct-user and group scopes are unioned. The result is then
intersected with the Agent's global PolarRAG scope and the user's visible
resources; PolarRAG document ACL remains the final authorization gate. If the
union exceeds `max_exhaustive_knowledge_resources`, exhaustive search fails
closed instead of silently omitting KBs.

## PAS user and enterprise-identity mappings

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| List user mappings | `GET /api/identity-sources/users/{user_id}/identities` | Path parameter `user_id`; `offset` (default `0`), `limit` (default `20`, maximum `100`), optional `search` | Returns `{items, total, offset, limit}` for enterprise identities associated with the PAS user, including user, department, group, and native PolarRAG principals. Search matches the stable external subject before pagination. |
| Bind enterprise identity | `POST /api/identity-sources/users/{user_id}/identities` | Path parameter `user_id`; body `identity_source_id`, `external_user_id` | Associates a synchronized enterprise user with an existing PAS user. If that identity belongs to its auto-created PAS user, the mapping is safely transferred. |
| Update mapping | `PUT /api/identity-sources/users/{user_id}/identities/{identity_id}` | Path parameters `user_id`, `identity_id`; body `identity_source_id`, `external_user_id` | Replaces a manually maintained mapping with another synchronized enterprise user. A synchronized user's primary identity cannot be edited. |
| Delete mapping | `DELETE /api/identity-sources/users/{user_id}/identities/{identity_id}` | Path parameters `user_id`, `identity_id` | Removes a manually maintained mapping and restores the enterprise identity's default synchronized user. A synchronized user's primary identity cannot be removed. |

Choose `external_user_id` from the stable external user IDs returned by the source-directory endpoint; never infer it from an email address or display name.

## External principal memberships and legacy Feishu snapshot

`PUT /api/identity-sources/{source_id}/user-principal-memberships` accepts up
to 100 users and 50,000 memberships. Each user entry supplies a complete
`principals` list (`principal_type`, `principal_id`, and optional `expires_at`);
an empty list clears that user and omitted users are
unchanged. PAS uses non-expired rows for ACL context and Agent group matching.
The user does not need to exist in the directory snapshot.

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| Configure membership snapshot | `PUT /api/identity-sources/{source_id}/acl-membership-snapshot` | Path parameter `source_id`; body `host`, optional `port`, optional `database`, `username`, `password` | Stores an ACL membership snapshot connection for a verified Feishu source. The source returns to pending binding and must sync again. |

This endpoint is only for Feishu deployments that have an ETL ACL membership snapshot. Ordinary Feishu and SharePoint directory synchronization does not need it. The password is write-only and must come from protected automation.
The local batch API and this legacy database-backed snapshot are mutually
exclusive. Either endpoint returns `409` with
`ACL_MEMBERSHIP_BACKEND_CONFLICT` when the other backend is already configured.
