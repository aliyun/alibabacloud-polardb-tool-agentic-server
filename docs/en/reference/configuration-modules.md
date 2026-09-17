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
- `user_sso`: optional OIDC or OAuth 2.0 UserInfo human login and trusted
  external access-token validation; depends on `token_security`.
- `aliyun_access`: mode-scoped Alibaba Cloud credentials, region, and
  `openapi_network`.
- **Service runtime policy** (`runtime_policy`): external URL, CORS,
  connection-pool, worker policy,
  global `delete_cooldown_duration_hours`, and the
  `dedicated_pool_enabled` activation flag.
- **SQL security policy** (`sql_security`): limits, blocked operations,
  confirmation, rate, and SQL timeouts.
- **PolarRAG runtime policy** (`polarrag_tool_limits`): process-local rate,
  burst, instance concurrency, fan-out governance, and upstream request timeout.
- `observability`: application logging and global audit lifecycle behavior.

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

## PolarRAG runtime policy

`polarrag_tool_limits` is independent from both `runtime_policy` and
`sql_security`. The service runtime policy controls PAS-wide connection pools,
configuration polling, and workers. The PolarRAG runtime policy controls
PolarRAG upstream request timeout plus MCP Tool admission and upstream fan-out.
It has these safe defaults:

- `enabled: true`;
- `user_requests_per_minute: 60` (range `1..60000`) and `user_burst: 10`
  (range `1..10000`);
- `agent_requests_per_minute: 120` (range `1..60000`) and `agent_burst: 20`
  (range `1..10000`);
- `instance_max_inflight: 16` (range `1..10000`);
- `max_fanout: 8` (range `1..1000`);
- `max_exhaustive_knowledge_resources: 1000` (range `1..10000`) for one
  binding snapshot or exhaustive search;
- `retry_after_seconds: 1` (range `1..3600`) for concurrency and fan-out
  rejections;
- `upstream_request_timeout_ms: 20000` (range `100..300000`) for each
  PolarRAG HTTP request.

Rate checks apply to both the PAS user and, for a user-specific Agent Token,
the Agent. Instance concurrency is keyed by `polarrag_instance_id`. All limits
are in process memory and apply independently in each PAS replica; with `N`
replicas, aggregate capacity is approximately `N` times the configured values.
This is intentionally not a distributed exact limit.

An administrator edits and activates this module on the existing service
configuration page. Active changes take effect after the normal runtime
configuration polling interval without a PAS restart. A changed rate or burst
setting resets local token buckets; already-running upstream calls keep their
reservations until completion, and new reservations use the active limits.
The request timeout also applies to newly constructed clients after the polling
interval. An in-flight request retains the timeout with which its client was
constructed. The setting is not a single deadline over a multi-request Tool
call.

## Global audit observability

The **Observability** (`observability`) module owns global audit settings in
addition to application log level, output, rotation, and timezone:

- `audit_enabled: true` controls optional audit records. Security-required
  audit events remain recorded when this switch is false;
- `audit_retention_days: 180` (minimum `1`) controls cleanup of all database
  audit records.

Log rotation fields control application log files and do not duplicate audit
retention. Existing installations migrate the effective audit values formerly
stored under `sql_security` during startup; the move does not reset customized
values.

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
`token_endpoint`. OIDC mode validates ID Tokens against that exact issuer.
OAuth 2.0 UserInfo mode supports providers that do not return an OIDC ID Token,
but requires an effective UserInfo endpoint.

### User SSO fields and activation

`user_sso` accepts:

- `browser_login_enabled`, which defaults to `true`; set it to `false` for a
  Token Exchange-only deployment;
- `protocol_mode`: `oidc` or `oauth2_userinfo`;
- provider connection: `discovery_url`, or manual `issuer`,
  `authorization_endpoint`, and `token_endpoint`;
- `userinfo_endpoint`, optional in OIDC mode and required in OAuth 2.0
  UserInfo mode, plus an effective `jwks_uri` required in OIDC mode;
- `client_id`, encrypted `client_secret`, and `scopes`;
- `provider_name`, `user_id_claim`, `display_name_claim`, `email_claim`, and
  `default_department`;
- `id_token_algorithms`, `idp_pkce`, and `userinfo_token_method`
  (`bearer_header`, `form_post`, or `query`);
- `external_token_trust`, containing the external provider adapter, optional
  expected audience, PAS access-token lifetime, and the separately controlled
  direct MCP compatibility switch.

Validation requires public HTTPS addresses, exact configured/discovered issuer
agreement, a reachable JWKS document with a non-empty `keys` array, a
10-second request timeout, no redirects, and JSON responses no larger than
1 MiB. Private, loopback, link-local, and otherwise unsafe destination
addresses are rejected during normal operation. For same-machine development,
`pas serve --local-sso-dev` permits the PAS external base HTTP origin only on
`localhost` or `127.0.0.1`, matching the listener bound to `127.0.0.1`.
Provider HTTP endpoints may additionally use `::1`. LAN, wildcard, other
`127/8`, hostname-lookalike, and non-loopback-resolving destinations remain
rejected. The callback is derived only from
`runtime_policy.external_base_url`:

```text
EXTERNAL_BASE_URL/auth/oidc/callback
```

With browser login enabled, network validation alone does not permit
activation. The administrator starts
`POST /api/config/user-sso/tests`, completes the returned browser
authorization URL, and polls `GET /api/config/user-sso/tests/{id}`. Activation
of `user_sso` must include the passed test's `sso_test_id` in addition to the
normal `validation_id` and `expected_revision`. The proof is single-use and is
bound to the administrator, revision, normalized digest, and expiry.

When `browser_login_enabled=false`, PAS still requires an HTTPS External Base
URL to identify the Token Exchange resources, but does not require a callback
URL, Discovery/authorization/token/JWKS endpoints, browser Client ID or secret,
or a browser login test. External token trust must be enabled with
`oauth2_introspection`, `oauth2_userinfo`, or `feishu`; activation follows
successful Provider validation directly. Introspection in this mode requires
dedicated PAS-to-provider `client_id` and `client_secret`, while standalone
UserInfo requires `external_token_trust.userinfo_endpoint`.

In Token Exchange-only mode, ordinary console login remains built-in. When
browser login is enabled, ordinary console login uses SSO. The separate
recovery endpoint accepts only an active built-in administrator and remains
rate-limited and audited.

### External access-token trust fields

`external_token_trust.enabled` permits registered clients to submit RFC 8693
requests to `/token`. The Authorization Server metadata advertises the Token
Exchange grant. `provider` selects one adapter:

- `oidc_jwt` uses the SSO issuer, discovery/JWKS, algorithms, claims, and
  Client ID. `expected_audience` overrides the Client ID as the required JWT
  audience.
- `oauth2_introspection` calls `introspection_endpoint` with
  `client_secret_basic` or `client_secret_post`. An optional
  `expected_audience` must appear in the active response. Optional nested
  `client_id` and `client_secret` configure dedicated PAS-to-provider
  credentials; otherwise PAS uses the browser SSO credentials. When
  `userinfo_endpoint` is configured, PAS calls it with the external Bearer
  Token and requires `sub`, `user_id`, and `pas_source_id`, with optional
  `union_id`. The UserInfo `sub` must match the Introspection `sub`. This mode
  also requires `identity_source_id` for the active, verified enterprise
  identity source whose directory contains `user_id`.
- `oauth2_userinfo` uses the effective `userinfo_endpoint`,
  `userinfo_token_method`, and configured identity claims.
- `feishu` accepts a legacy static `identity_source_id`, but Token Exchange
  callers may instead provide `identity_source_id`, `feishu_user_id`, and
  `feishu_union_id` per request. PAS requires all three direct values together,
  loads an active, verified Feishu source, and compares the supplied values with
  Feishu UserInfo before issuing a token. Direct MCP remains unavailable with
  request identity context because `/mcp` has no equivalent form fields; it
  requires the legacy static identity source.
- `buc` requires `protocol_mode=oauth2_userinfo`,
  `userinfo_token_method=form_post`, and `user_id_claim=account_id`. PAS also
  requires the returned `client_id` to equal the configured Client ID.

`access_token_ttl_seconds` defaults to `28800` (8 hours) and accepts `60..86400`. The
effective PAS access-token lifetime is capped by a known external-token expiry.
`direct_mcp_enabled` defaults to `false` and cannot be true unless external
trust is enabled. When enabled, an unrecognized Bearer Token at `/mcp` is
validated as an external token on every request. The direct path maps the PAS
User and Workspace but returns no PAS access or refresh token. Standard Token
Exchange is accepted at both `/token` and `/api/v1/external-auth/token`; it
returns a PAS access token without a refresh token.

The Introspection and external UserInfo endpoints may use HTTP when every
resolved address is in an approved private network range. This supports
isolated VPC and intranet deployments, but sends Provider credentials and
external tokens without transport encryption; HTTPS remains recommended.
Public endpoints must use HTTPS. Link-local, metadata, multicast, unspecified,
reserved, mixed public/private, and unauthorized loopback destinations remain
rejected. Browser SSO endpoints continue to require the stricter public HTTPS
policy. Requests retain the existing timeout, redirect, and response-size
controls. Provider credentials and external tokens are never returned by
describe/export APIs or included in validation logs.

After this Provider configuration is active, administrators register calling
servers under **External Applications**. PAS creates a confidential OAuth
client, derives MCP and HTTP API resource profiles from the external base URL,
applies the selected Agent policy, and displays the client secret only at
creation or rotation. Provider configuration controls how PAS validates the
external token; external application registration controls which caller may
exchange it and which PAS targets and Agent selection rules it may use.
When validation is not active, **External Applications** links directly to the
`user_sso` module and opens **External access token trust**.

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
