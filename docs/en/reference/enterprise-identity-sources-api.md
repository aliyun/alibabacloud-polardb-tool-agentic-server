# Enterprise identity source administrator API

[简体中文](../../zh-cn/reference/enterprise-identity-sources-api.md)

This page describes administrator automation APIs for enterprise identity sources. Prefer the console workflow in [Enterprise identity sources](../administration/enterprise-identity-sources.md); use these APIs from scripts, operations platforms, or integrations.

Every endpoint is under `/api` and requires an administrator Session Cookie with `X-PAS-CSRF: 1`, or a supported administrator Bearer Token. Secret fields are write-only and no response returns a plaintext secret.

## Identity sources

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| Create source | `POST /api/identity-sources` | `name`, `provider`, optional `stale_after_seconds`; Feishu uses `app_id`, `app_secret`; SharePoint uses `tenant_id`, `cloud`, `client_id`, `client_secret` | Creates a pending-verification or pending-binding source. `provider` is `feishu` or `sharepoint`. |
| Start Feishu tenant verification | `POST /api/identity-sources/{source_id}/feishu-verification` | Path parameter `source_id` | Returns a one-time `authorization_url` for the administrator to open in a browser. Available only to pending Feishu sources. |
| List sources | `GET /api/identity-sources` | None | Returns source state, bound Spaces, latest sync time, and sanitized error type. |
| Sync now | `POST /api/identity-sources/{source_id}/sync` | Path parameter `source_id` | Refreshes a ready Feishu or SharePoint directory snapshot immediately. |
| Update source | `PUT /api/identity-sources/{source_id}` | Path parameter `source_id`; `name` and complete current-provider credentials. Feishu uses `app_id`, `app_secret`; SharePoint uses `cloud`, `client_id`, `client_secret` | Updates credentials and clears the latest sync state. Feishu must verify the tenant again; SharePoint retains its tenant ID and syncs again after binding. |
| Delete source | `DELETE /api/identity-sources/{source_id}` | Path parameter `source_id` | Deletes the source, directory snapshot, Space bindings, and source-derived grants. Existing PAS users remain. |
| Inspect synchronized directory | `GET /api/identity-sources/{source_id}/directory` | Path parameter `source_id` | Returns up to 200 synchronized users and 200 synchronized groups for operations checks and user-identity selection. |

`source_id` is the identity-source UUID returned by creation or listing. A failed sync exposes only a sanitized error type; inspect protected PAS logs without recording or transmitting secrets.

## Space bindings

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| List bindable Spaces | `GET /api/identity-sources/spaces` | None | Returns enabled PolarRAG Spaces with `knowledge_space_id`, name, and `identity_domain`. |
| Bind Space | `POST /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | Path parameters `source_id`, `knowledge_space_id` | Makes synchronized principals eligible for ACL-context construction in that Space. Repeated calls are idempotent. |
| Unbind Space | `DELETE /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | Path parameters `source_id`, `knowledge_space_id` | Immediately stops contributing source principals to that Space without deleting the source or synchronized users. |

A binding makes principals ACL candidates; it does not grant knowledge-base or document access. PolarRAG ACL remains the final decision.

## Agent enterprise access orchestration

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| Preview Agent enterprise access | `POST /api/agents/{agent_id}/enterprise-access/preview` | `identity_source_id`, explicit `all_synced_users`, `directory_group_ids`, `pas_user_ids`, `knowledge_space_ids` | Performs read-only authoritative validation and returns normalized `selection`, Agent-local `creates`, global Source-Space `global_changes`, existing `reuses`, and `preview_hash`. |
| Apply reviewed access | `POST /api/agents/{agent_id}/enterprise-access/apply` | The exact returned selection plus `preview_hash` | Atomically and idempotently creates missing Source-Space bindings and Agent subject grants, then records required audit entries. Any validation, write, commit, or audit failure rolls back the complete operation. |

Requests contain only PAS internal source, directory-row, user, and Space IDs.
They must not contain `provider`, `principal_id`, `identity_domain`, credentials,
or `acl_context`; PAS derives these trusted values from current server-side
records. `all_synced_users` defaults to `false`, but clients should always send
the current explicit choice. At least one subject and one Space are required.
Each request accepts at most 500 `directory_group_ids`, 500 `pas_user_ids`, and
200 `knowledge_space_ids`.

Apply revalidates current Source freshness, directory state, Space state,
Agent-instance bindings, and the previewed relationships. If anything changed,
it writes nothing and returns `409` with
`detail.code=ENTERPRISE_ACCESS_PREVIEW_STALE` and a refreshed preview. The
client must show that preview and obtain a new confirmation; it must not
automatically apply it. New Source-Space bindings are global shared state.
Removing a resulting Agent user, group, or all-users grant later does not
remove those global bindings, Agent-instance bindings, or shared PUBLIC scope.

## PAS user and enterprise-identity mappings

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| List user mappings | `GET /api/identity-sources/users/{user_id}/identities` | Path parameter `user_id` | Returns enterprise identities associated with the PAS user, including user, department, group, and native PolarRAG principals. |
| Bind enterprise identity | `POST /api/identity-sources/users/{user_id}/identities` | Path parameter `user_id`; body `identity_source_id`, `external_user_id` | Associates a synchronized enterprise user with an existing PAS user. If that identity belongs to its auto-created PAS user, the mapping is safely transferred. |
| Update mapping | `PUT /api/identity-sources/users/{user_id}/identities/{identity_id}` | Path parameters `user_id`, `identity_id`; body `identity_source_id`, `external_user_id` | Replaces a manually maintained mapping with another synchronized enterprise user. A synchronized user's primary identity cannot be edited. |
| Delete mapping | `DELETE /api/identity-sources/users/{user_id}/identities/{identity_id}` | Path parameters `user_id`, `identity_id` | Removes a manually maintained mapping and restores the enterprise identity's default synchronized user. A synchronized user's primary identity cannot be removed. |

Choose `external_user_id` from the stable external user IDs returned by the source-directory endpoint; never infer it from an email address or display name.

## Feishu ACL membership snapshot

| Operation | Method and path | Request parameters | Effect |
| --- | --- | --- | --- |
| Configure membership snapshot | `PUT /api/identity-sources/{source_id}/acl-membership-snapshot` | Path parameter `source_id`; body `host`, optional `port`, optional `database`, `username`, `password` | Stores an ACL membership snapshot connection for a verified Feishu source. The source returns to pending binding and must sync again. |

This endpoint is only for Feishu deployments that have an ETL ACL membership snapshot. Ordinary Feishu and SharePoint directory synchronization does not need it. The password is write-only and must come from protected automation.
