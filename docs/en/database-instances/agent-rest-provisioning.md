# Agent REST database provisioning

[简体中文](../../zh-cn/database-instances/agent-rest-provisioning.md)

This guide is the human-readable contract for Agents that create, inspect,
and delete PAS-managed databases over REST. The machine-readable contract is
served by the same PAS deployment at `/mcp/rest/openapi.json`.

## Authentication and isolation

Send the Agent Token only in the HTTP authorization header:

```http
Authorization: Bearer <agent-token>
Content-Type: application/json
```

The Token represents one Agent. An Agent can see only its own resources; an
unknown resource and another Agent's resource both return
`RESOURCE_NOT_FOUND`. Human JWTs, administrator cookies, and bootstrap tokens
cannot call these routes. Conversely, an Agent Token cannot call the
human-user routes under `/mcp/rest` or administrator routes under `/api`.

All lifecycle responses use `Cache-Control: no-store`. Never put the Token,
database password, or full response body in a URL, log, trace attribute,
prompt, analytics event, or persistent cache.

## Discover the contract

- The Agent detail page shows the lifecycle URL and links to both the live API
  documentation and this guide. It reuses the same Agent Token shown for MCP.
- `GET /mcp/rest/openapi.json` returns only the Agent lifecycle schema.
- `GET /mcp/rest/docs` renders that scoped schema for a human reader.
- The application-level `/openapi.json` intentionally excludes the Agent
  lifecycle routes.

Use the schema from the deployed PAS version as authoritative. The lifecycle
base path reuses the MCP REST port; the authentication principal and entry
point are different, while the internal lifecycle implementation is shared
with the MCP Tools. The GitHub guide is supplementary and can describe a newer
released version than the PAS deployment currently serving the Agent page.

## Create a database

Call `POST /mcp/rest/db-instances` with a permanent `client_token`, the engine,
and an explicit provisioning mode:

```json
{
  "client_token": "job-018f7f2d",
  "name": "orders-sandbox",
  "db_type": "polardb_mysql",
  "provisioning_mode": "dedicated"
}
```

`provisioning_mode` is required and accepts `multitenant` or `dedicated`.
Dedicated operation budgets apply only to Dedicated provisioning because a
Dedicated create/delete loop can purchase physical clusters. Multitenant
operations remain bounded by active-resource quotas and do not trigger a
physical purchase; this asymmetry is intentional.

`client_token` is scoped to the Agent and remains bound to the normalized
request permanently. Replaying the same request returns the same
`resource_id`, including after `DELETED`. Reusing the key for different input
returns `IDEMPOTENCY_CONFLICT`.

A hot Dedicated allocation returns `201` and `READY`. A cold allocation
returns `202`, `CREATING`, `Location`, `Retry-After`, and
`retry_after_seconds`. Capacity and operation-budget rejection returns `429`
without weakening delete or disconnect safety.

## Poll and use the database

Poll `GET /mcp/rest/db-instances/{resource_id}` at or after the indicated
interval. The status set is `CREATING`, `READY`, `FAILED`, `DELETING`,
`COOLING_DOWN`, `RESTORING`, `DELETED`, and `DELETE_FAILED`.

Only `READY` includes `connection`:

```json
{
  "resource_id": "00000000-0000-0000-0000-000000000000",
  "status": "READY",
  "provisioning_mode": "dedicated",
  "name": "orders-sandbox",
  "db_type": "polardb_mysql",
  "source": "provisioned",
  "connection": {
    "host": "example.mysql.polardb.rds.aliyuncs.com",
    "port": 3306,
    "database": "agentic_example",
    "username": "agentic_example",
    "password": "<database-password>"
  }
}
```

Treat connection fields as one secret bundle and keep it only as long as the
workload needs it. `FAILED` is terminal for creation, returns no credentials,
and remains stable under an idempotent replay.

## Delete and cooldown

Call `DELETE /mcp/rest/db-instances/{resource_id}` when the work is complete.
The request is idempotent: a permanently deleted resource returns `204`.

PAS first revokes resource access and disconnects existing sessions. For
Dedicated resources, `cooldown_until` starts only after disconnection is
verified. The effective `delete_cooldown_duration_hours` comes from the most
specific resource/member override, then pool override, then the global value;
the global default is 24 hours and the intentional minimum is 1 hour.

During `COOLING_DOWN`, the member does not count toward the pool's planning
capacity but still counts toward billable `max_total_members`. The Agent cannot
restore a resource. An administrator may restore a reversible resource from
`DELETING`, `DELETE_FAILED`, or `COOLING_DOWN`; PAS re-enables the original
database and account credentials only after verification succeeds. After
sanitize or physical destruction begins, restore is no longer available.

## Errors and retry behavior

Errors have a stable `code`, sanitized `message`, and `request_id`.
Retryable responses also include `Retry-After` and `retry_after_seconds`.
Handle at least these codes:

- `INVALID_ARGUMENT` and `UNSUPPORTED_PROVISIONING_MODE`: fix the request.
- `NO_ELIGIBLE_BACKEND`: ask an administrator to enable and bind a healthy
  backend for the requested mode.
- `POOL_CAPACITY_LIMIT_REACHED`: wait for capacity or an administrator change;
  do not loop create/delete.
- `RATE_LIMITED`: wait at least the returned interval.
- `IDEMPOTENCY_CONFLICT`: use the original request or a new `client_token`.
- `RESOURCE_STATE_CONFLICT`: refresh the resource before retrying an action.
- `PROVISIONING_FAILED` and `DISCONNECT_FAILED`: preserve `request_id` and ask
  an operator to inspect sanitized server logs.
- `RESOURCE_NOT_FOUND` and `UNAUTHORIZED`: do not probe other identifiers or
  retry with a human credential.

Use exponential backoff with jitter for transient failures. A delete request
must remain possible even when create/delete rate or purchase budgets are
exhausted, because access revocation takes priority over quota enforcement.

## Agent workflow example

The following sequence is safe for both provisioning modes:

```text
POST create with a new client_token
if CREATING: poll Location using Retry-After
if READY: use connection without persisting its password
DELETE the resource in a finally/cleanup path
poll only when the caller needs to observe disconnection or final cleanup
```

MCP Agents may instead use `create_db_instance`, `describe_db_instance`, and
`delete_db_instance`; REST and MCP share the same resource, idempotency,
permission, cooldown, and cleanup implementation.
