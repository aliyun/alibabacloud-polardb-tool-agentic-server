# REST API reference

[简体中文](../../zh-cn/reference/rest-api.md)

The Web console uses the authenticated REST API under `/api`. MCP clients
use the Streamable HTTP endpoint `/mcp`. The `/mcp/rest` prefix contains both
legacy human-user SQL routes and the Agent database lifecycle routes; each
route family enforces its own principal type.

## Authentication and safety

Human administration uses an administrator session cookie plus `X-PAS-CSRF:
1`, or a supported administrator Bearer token. Agent Tokens cannot call
administrator APIs. During setup, `POST /api/config` accepts a valid
`Authorization: Bootstrap ...` claim only.

Responses use stable error codes and sanitized messages. Do not rely on raw
exception text. Mutating configuration activation and disable actions require
idempotency and revision controls.

## Main resources

Administrative routes include `/api/users`, `/api/departments`,
`/api/instances`, `/api/agents`, `/api/credentials`,
`/api/provisioning-backends`, and `/api/audit-logs`.
Nested user and Agent routes manage instance/provisioning bindings and owned
resources. Dedicated administration adds `/api/dedicated-pools`,
`/api/permission-templates`, `/api/permission-template-revisions/{id}/sync`,
`/api/permission-sync-jobs`, and `/api/db-instance-resources`.

`/api/polarrag` provides administrator-only PolarRAG instance checks, trusted
Space enablement and synchronization, and per-user enterprise principal
assignments. Secret fields are write-only. See
[PolarRAG MCP and enterprise identities](../knowledge/polarrag-mcp.md).

The [Enterprise identity source administrator API](enterprise-identity-sources-api.md)
separately lists the management operations, parameters, and effects for Feishu
and SharePoint sources, Space bindings, user mappings, and Feishu ACL
membership snapshots.

Agent-scoped PolarRAG access is managed under
`/api/agents/{agent_id}/polarrag-bindings` and
`/api/agents/{agent_id}/user-assignments`. An authenticated user lists their
assignments at `/api/me/agent-connections` and manages their own Token with the
nested `issue`, `reveal`, `regenerate`, and `revoke` operations. Sensitive
responses use `Cache-Control: no-store`. Reveal uses the current authenticated
built-in or SSO session without a second password request and remains
owner-scoped, audited, and rate-limited; administrator responses never contain
user Token plaintext.

### User workspace

`GET /api/me/workspace` returns the authenticated User's workspace, current
default Agent, and available active Agents. Its `status` is one of:

- `ready`: the selected default Agent remains available;
- `selection_required`: more than one Agent is available and none is selected;
- `no_agent_access`: the User has no active authorized Agent;
- `default_agent_unavailable`: the saved Agent is disabled or no longer
  authorized.

When exactly one Agent is available, the GET operation selects it
automatically. `PUT /api/me/workspace/default-agent` accepts
`{"agent_id": "..."}` and selects only an Agent currently available to the
User. PAS preserves an unavailable saved ID and refuses MCP tool execution; it
does not silently select a replacement.

### SSO configuration test

After saving and validating a `user_sso` draft,
`POST /api/config/user-sso/tests` returns `id`, `status`, `authorize_url`, and
`expires_at`. The current administrator opens `authorize_url` in a browser and
then polls `GET /api/config/user-sso/tests/{id}` for `pending`, `exchanging`,
`passed`, or `failed`.

The normal `POST /api/config` activation command for `user_sso` adds
`sso_test_id`:

```json
{
  "protocol_version": 1,
  "action": "activate",
  "module": "user_sso",
  "expected_revision": 4,
  "validation_id": "VALIDATION_ID",
  "sso_test_id": "SSO_TEST_ID",
  "idempotency_key": "REQUEST_ID"
}
```

The test is owner-scoped, expires, becomes stale when the configuration
changes, and is consumed atomically during activation.

For one binding, administrators list eligible synchronized ACTIVE PUBLIC
resources with
`GET /api/agents/{agent_id}/polarrag-bindings/{binding_id}/public-resources`
and update the scope with `PUT` on the same route. The request field
`public_knowledge_resource_ids` is `null` for all instance PUBLIC resources, an
array for a selected scope, and an empty array for no PUBLIC resources.
PERSONAL IDs are rejected. Updates are audited as
`agent_polarrag_binding.public_scope.update` and apply to the next MCP Tool call
without Token regeneration.

Manual Agent knowledge bindings are changed atomically with
`POST /api/admin/agents/{agent_id}/knowledge-bindings:batch`; the compatibility
mode is changed with `PUT /api/admin/agents/{agent_id}/knowledge-scope-mode`.
Identity-source automation uses the idempotent
`PUT/DELETE /api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}`
routes. Local external principal snapshots use
`PUT /api/identity-sources/{source_id}/user-principal-memberships`.

Administrators set a knowledge resource to `NATIVE` or `EXTERNAL_SYNC` with
`PUT /api/polarrag/knowledge-resources/{knowledge_resource_id}/management-mode`.
PAS user and MCP document writes are rejected for `EXTERNAL_SYNC` resources.

Instance registration has connection-test endpoints before creation and on an
existing instance. Credential creation/update has its own test action.
Connection tests execute from the backend Pod.

The former pool and human quota routers are retired. Human instance requests
no longer auto-purchase physical clusters; an unassigned User receives
`NO_INSTANCE_ASSIGNED` and should contact an administrator.

## Agent database lifecycle

Agent Bearer Tokens call `POST /mcp/rest/db-instances`,
`GET /mcp/rest/db-instances/{resource_id}`, and
`DELETE /mcp/rest/db-instances/{resource_id}`. Human JWTs and cookies are
rejected. Responses use `Cache-Control: no-store`; creation and retryable
states provide `Location` or `Retry-After` where applicable.

See [Agent REST database provisioning](../database-instances/agent-rest-provisioning.md)
for examples, status semantics, idempotency, errors, and credential handling.

## Guided configuration

`POST /api/config` uses one versioned command envelope:

```json
{
  "protocol_version": 1,
  "action": "describe",
  "module": "runtime_policy"
}
```

Actions include `describe`, `plan`, `save_draft`, `validate`, `activate`,
`skip`, `reset`, `disable`, and `export`. Side effects require fields specified
by the command contract, including `expected_revision`, validation proof, or
idempotency key where applicable. Activating `user_sso` also requires the
successful browser proof described above.

## Internal lifecycle API

When an operator explicitly enables the separate management listener, it
provides `GET /api/internal/v1/status` and
`POST /api/internal/v1/config`, plus the managed account routes described
below. This listener is opt-in and isolated: its routes are not part of the
customer REST API or the generated business-listener OpenAPI. Do not expose
them through the business Service, Ingress, or public API gateway.

The phase-one mutation allowlist accepts one `core_admin` parameter per
command: `username` during managed initialization and `password` only for the
one-time `RESET_REQUIRED` to `ACTIVE` transition. Managed `save_draft`,
`validate`, `activate`, and `set_initial_password` commands require an
idempotency key. A new password operation after activation returns
`ADMIN_PASSWORD_ALREADY_INITIALIZED`; normal later changes require an
authenticated PAS session and the current password.

### Managed administrator account

The listener manages one fixed built-in account, `admin`. Use
`GET /api/internal/v1/accounts` with the bound `instance_id` and `generation`;
an optional `account_name` must be `admin`. The response identifies the account
and its states with `account_name`, `account_status`, and `password_status`.
It never includes a password.

Use `POST /api/internal/v1/accounts/admin/password` to change that account's
password. Its versioned envelope includes `target` (`instance_id` and
`generation`), a `control_user` actor, `operation`, `new_password`, and an
`idempotency_key`. `protocol_version` is required and must be `1`; PAS does not
default an omitted version. The operation controls the old-password requirement:

| Operation | Allowed password state | Old-password rule |
| --- | --- | --- |
| `MODIFY` | `ACTIVE` | `old_password` is required and must be the current password. |
| `RESET` | `RESET_REQUIRED` or `ACTIVE` | `old_password` is not accepted. |

The existing `set_initial_password` configuration command remains the separate
first-only initialization path from `RESET_REQUIRED` to `ACTIVE`. Repeating it
after activation returns `ADMIN_PASSWORD_ALREADY_INITIALIZED`.

Every successful built-in password initialization, modification, or reset
increments the credential epoch, revokes REST and MCP OAuth refresh tokens,
invalidates outstanding built-in authorization codes, and rejects existing
password-authenticated access sessions. Passwords are never returned by any
endpoint, including successful account responses, idempotency replays, or
errors. Reusing an idempotency key with a different request continues to return
the PAS code `IDEMPOTENCY_CONFLICT`.

PAS validates the submitted actor against the `control_user` model and includes
that actor, together with the rest of the request envelope, in the
purpose-separated HMAC used for idempotency comparison. Management-listener
authentication and the configured instance ID and generation establish the
trusted boundary; PAS does not reconstruct the submitted actor. Responses,
receipts, and logs never contain the supplied password. Use the customer OpenAPI
and UI for customer operations; never call this internal listener from a
customer client.

## OpenAPI discovery

The application schema at `/openapi.json` excludes Agent lifecycle routes.
Agents use the canonical scoped schema at `/mcp/rest/openapi.json`; humans can
view `/mcp/rest/docs`. CI verifies both surfaces so a principal cannot discover
or accidentally use the other contract. Treat the deployed version's schema
as authoritative and use the immutable release tag's documentation for
examples.

## Account/resource console APIs

| Method | Path | Access |
| --- | --- | --- |
| GET | `/api/access/accounts?kind=personal` | Admin; `kind=service` selects service accounts |
| GET | `/api/access/resources?kind=database` | Admin; `kind=knowledge` respects knowledge availability |
| GET | `/api/access/grants` | Admin; filter `resource_id` or paired `account_kind`, `account_id` |
| PUT | `/api/access/accounts/{kind}/{account_id}/resources/{instance_id}` | Admin; SQL preset, `credential_id` and `permission` |
| POST | `/api/access/accounts/{kind}/{account_id}/resources/{instance_id}/disable` | Admin; retain disabled direct binding |
| GET | `/api/me/resources?personal=true` | Current user's resource scope, no Agent selection |
| GET / POST | `/api/me/personal-tokens` | Current user only; POST accepts `expires_in_days` (1–365, default 90) |
| DELETE | `/api/me/personal-tokens/{token_id}` | Current token owner only |

Account/resource lists and grants use `offset`, `limit` pagination; account and resource lists support `search`. Grant `kind` is `personal` or `service`. Personal token plaintext is returned once on POST; GET returns status only. These endpoints reuse existing identities and binding storage.
