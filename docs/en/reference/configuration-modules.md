# Configuration module reference

[简体中文](../../zh-cn/reference/configuration-modules.md)

Runtime configuration is stored in encrypted, revisioned module documents in
the metadata database. Only `PAS_DATABASE_URL` and `PAS_ENCRYPTION_KEY` remain
process bootstrap settings.

## Module catalog

- `token_security`: shared JWT key ring and token lifetimes.
- `core_admin`: first built-in administrator; depends on `token_security`.
- `agent_token_auth`: required, active-by-default Agent Bearer Token issuance
  and authentication for MCP and Agent REST APIs. It is independent of human
  administrator authentication and cannot be skipped or disabled.
- `user_sso`: optional OIDC human login; depends on `token_security`.
- `aliyun_access`: mode-scoped Alibaba Cloud credentials, region, and
  `openapi_network`.
- `runtime_policy`: external URL, CORS, connection-pool, worker policy,
  global `delete_cooldown_duration_hours`, and the
  `dedicated_pool_enabled` activation flag.
- `sql_security`: limits, blocked operations, confirmation, rate, and audit.
- `observability`: log and audit retention behavior.

Optional modules can remain `SKIPPED`. Disable active dependents before their
dependency.

Auto-provisioning pool placement, sizing, permissions, and routing are resources
managed on the **Pool** and **Agent** pages, not configuration modules. The old
`resource_pool` module is retired and rejected by configuration commands.

The server-owned `agentic-dedicated-mysql` profile is the single source of
fixed `CreateDBCluster` parameters for AgenticDB Dedicated clusters. A pool
selects placement and a storage type accepted by that profile; administrators
do not maintain a second purchase-parameter module.

## Auto-provisioning worker runtime safety

The **Service runtime policy** (`runtime_policy`) module exposes these
auto-provisioning worker controls:

- `dedicated_pool_enabled` starts or stops real Dedicated lifecycle work. Each
  replica's in-process supervisor applies the active revision online; changing
  it does not require a PAS restart or rollout.
- `dedicated_worker_heartbeat_interval_seconds` defaults to 10 seconds.
- `dedicated_worker_heartbeat_stale_after_seconds` defaults to 30 seconds and
  must be at least both three heartbeat intervals and 30 seconds. Heartbeats
  and freshness comparisons use the metadata database clock.
- `dedicated_pool_simulation_enabled` defaults to false. It is an explicit
  development/test fallback that creates labeled simulated members only when
  no active Alibaba Cloud credential exists. Active credentials take
  precedence. Missing Alibaba Cloud access fails closed when this switch is
  false; never leave it enabled in production.
- `dedicated_pool_preparation_mode` defaults to `full`. `openapi_only` creates
  real billable cloud resources but pauses at durable `OPENAPI_READY` before
  private MySQL grants and verification. Changing it back to `full` resumes
  those members online without restarting PAS or purchasing again.

At this pause boundary, the pool member remains `REPLENISHING`. If the member
was created for an Agent cold request, the Agent-facing database resource also
remains `CREATING`; it must not expose credentials or become allocatable. The
underlying PolarDB cluster is nevertheless running and billable.

The console readiness endpoint reports durable active-worker evidence and
simulation and preparation modes. A newly enabled worker may need one heartbeat interval to
appear. Enabling the module does not prove that its Alibaba Cloud identity can
call `CreateDBCluster`.

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

For `user_sso`, OIDC discovery supplies the trusted issuer. Manual endpoint
configuration requires an explicit `issuer`, `authorization_endpoint`, and
`token_endpoint`; PAS validates ID Tokens against that exact issuer.

## Alibaba Cloud credential modes

`aliyun_access` has exactly three modes:

- `direct_ak` uses an encrypted long-lived AccessKey ID and Secret directly.
- `assume_role` stores an encrypted, low-privilege source AccessKey ID and
  Secret in PAS, then assumes the RAM role and uses a temporary identity
  credential (STS Token, also called STS temporary credentials) for OpenAPI
  calls.
- `ecs_ram_role` stores no AccessKey. PAS must be deployed on an ECS instance
  authorized with the RAM role. The runtime uses IMDSv2 only; IMDSv1 fallback,
  an arbitrary metadata URL, and a second role-assumption chain are not
  supported.

Only the selected block is decrypted. Temporary credentials remain in process
memory and are never persisted, returned in an API response, exported, logged,
or sent to an Agent or sandbox. AccessKey IDs are encrypted at rest and use a
short server-generated display mask; AccessKey Secrets and External IDs only
have configured-state markers. The dry-run result can include a masked identity
or role, temporary expiry, and a safe Alibaba Cloud Request ID.

Switching modes requires confirmation when the current mode has a stored
credential. **Clear the previous credential** is the default. **Retain it, but
keep it disabled** keeps the old block encrypted
but inactive; it is not decrypted or reused automatically. To reactivate it,
choose **Use retained credential** or enter a replacement, then validate and
explicitly enable it. **Delete retained credential** permanently removes that
inactive block. Reusing a retained direct AccessKey as an AssumeRole source is
also a separate, confirmed action.

For `direct_ak`, dry run reads PolarDB metadata. For `assume_role`, it first
performs AssumeRole and then reads PolarDB with the resulting temporary
credential. For `ecs_ram_role`, it obtains the ECS role through IMDSv2 before
the same read-only check. The PolarDB preflight calls `DescribeDBClusters`, so
it proves cluster-query access only. It does not prove that `CreateDBCluster`
or other permissions required by auto-provisioning pool purchasing are granted.
Grant those permissions before enabling automatic supply; otherwise the pool can
fail to create an instance. `OPENAPI_PERMISSION_DENIED`
means the active credential reached PolarDB but lacks the required
authorization; use the troubleshooting guide rather than widening permissions
blindly.

## Secrets and export

Secret fields are encrypted independently under the root key. Omitting an
existing secret preserves it; an explicit supported clear action removes it.
Describe and export responses contain configured/redacted markers only.
