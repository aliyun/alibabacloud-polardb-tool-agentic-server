# Configuration module reference

[简体中文](../../zh-cn/reference/configuration-modules.md)

Runtime configuration is stored in encrypted, revisioned module documents in
the metadata database. Only `PAS_DATABASE_URL` and `PAS_ENCRYPTION_KEY` remain
process bootstrap settings.

## Module catalog

- `token_security`: shared JWT key ring and token lifetimes.
- `core_admin`: first built-in administrator; depends on `token_security`.
- `agent_token_auth`: administrator-issued Agent Token capability.
- `user_sso`: optional OIDC human login; depends on `token_security`.
- `aliyun_access`: AccessKey or AssumeRole credentials, region, and
  `openapi_network`.
- `agentic_db_purchase`: PolarDB purchase specification; depends on
  `aliyun_access`.
- `resource_pool`: VPC placement and pool policy; depends on
  `agentic_db_purchase`.
- `runtime_policy`: external URL, CORS, connection-pool, and worker policy.
- `sql_security`: limits, blocked operations, confirmation, rate, and audit.
- `observability`: log and audit retention behavior.

Optional modules can remain `SKIPPED`. Disable active dependents before their
dependency.

## Workflow states

The lifecycle uses `NOT_CONFIGURED`, `DRAFT`, `VALIDATING`, `VALIDATED`,
`ACTIVE`, `ERROR`, `DISABLED`, and `SKIPPED`. Edits create a draft without
changing the effective snapshot. Validation produces a short-lived proof bound
to revision, normalized digest, and dependency revisions. Activation requires
that proof and an expected revision.

## External validation

Only modules with external dependencies perform network I/O. For
`aliyun_access`, the backend Pod sends a read-only PolarDB metadata request and
first calls STS in AssumeRole mode. Results contain resolved endpoint/status
and sanitized failure codes, never credentials or raw SDK exceptions.

`openapi_network` accepts only `public` or `vpc`; custom hostnames are rejected.

## Secrets and export

Secret fields are encrypted independently under the root key. Omitting an
existing secret preserves it; an explicit supported clear action removes it.
Describe and export responses contain configured/redacted markers only.
