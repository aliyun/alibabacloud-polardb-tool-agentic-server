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

Agent-scoped PolarRAG access is managed under
`/api/agents/{agent_id}/polarrag-bindings` and
`/api/agents/{agent_id}/user-assignments`. An authenticated user lists their
assignments at `/api/me/agent-connections` and manages their own Token with the
nested `issue`, `reveal`, `regenerate`, and `revoke` operations. Sensitive
responses use `Cache-Control: no-store`; administrator responses never contain
user Token plaintext.

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
idempotency key where applicable.

## OpenAPI discovery

The application schema at `/openapi.json` excludes Agent lifecycle routes.
Agents use the canonical scoped schema at `/mcp/rest/openapi.json`; humans can
view `/mcp/rest/docs`. CI verifies both surfaces so a principal cannot discover
or accidentally use the other contract. Treat the deployed version's schema
as authoritative and use the immutable release tag's documentation for
examples.
