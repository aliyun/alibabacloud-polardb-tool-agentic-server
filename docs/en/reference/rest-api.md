# REST API reference

[简体中文](../../zh-cn/reference/rest-api.md)

The Web console uses the authenticated REST API under `/api`. MCP clients
should use the Streamable HTTP endpoint `/mcp`; `/mcp/rest` is the legacy
human-user SQL surface and is not the Agent provisioning API.

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
`/api/provisioning-backends`, `/api/audit-logs`, `/api/quota`, and `/api/pool`.
Nested user and Agent routes manage instance/provisioning bindings and owned
resources.

Instance registration has connection-test endpoints before creation and on an
existing instance. Credential creation/update has its own test action.
Connection tests execute from the backend Pod.

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

When enabled by deployment policy, FastAPI exposes generated OpenAPI metadata
for request/response schemas. Treat the deployed version's schema as
authoritative and use the immutable release tag's documentation for examples.
