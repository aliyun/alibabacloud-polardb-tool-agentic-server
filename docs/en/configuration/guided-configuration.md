# Guided modular configuration

**English** | [简体中文](../../zh-cn/configuration/guided-configuration.md)

This guide covers optional modules and safe configuration changes after the
service has started.

## Before you begin

Complete [initial setup](../setup/initial-setup.md) first. That guide defines
the `PAS_DATABASE_URL` and `PAS_ENCRYPTION_KEY` bootstrap contract, database
migration, first administrator, Docker and Kubernetes token delivery, and
recovery.

This guide assumes the setup UI or `pas config init` has established ownership.
Administrators can open **Service Configuration** (`/settings/configuration`)
in the console to review or change modules. Visiting `/setup` after ownership
is established redirects to
that authenticated page. You may also use the interactive and declarative CLI
commands below.

## Modules and dependencies

`core_admin` and `token_security` establish the administrator and shared JWT
key ring. `agent_token_auth`, `runtime_policy`, `sql_security`, and
`observability` begin active with materialized safe defaults.

The remaining capabilities are modular:

- `agent_token_auth` is the required built-in capability that issues and
  authenticates administrator-managed Agent Bearer Tokens for MCP and Agent
  REST APIs. It is independent of human administrator sign-in and cannot be
  skipped or disabled.
- `user_sso` enables human OIDC sign-in and may remain `SKIPPED`.
- `aliyun_access` owns encrypted Alibaba Cloud credentials, the region, and
  `openapi_network` (`public` or `vpc`) selecting the reviewed PolarDB and
  STS OpenAPI endpoint families. It supports `direct_ak`, `assume_role`, and
  `ecs_ram_role` credential modes.
Auto-provisioning pool placement, capacity, permissions, and Agent routing are
not configuration modules. The server-owned `agentic-dedicated-mysql` profile
is the only source of fixed `CreateDBCluster` parameters. Each pool owns its
region, zone, VPC, VSwitch, and allowlisted storage type for AgenticDB
Dedicated clusters. Manage pools on
**Pool** after activating `aliyun_access`; network identifiers are entered
manually because PAS does not request extra RAM permissions to enumerate them.

**Service runtime policy** (`runtime_policy`) contains the global PAS runtime
settings, including `dedicated_pool_enabled`. The pool wizard links directly
to `/settings/configuration?module=runtime_policy` when the auto-provisioning
worker is disabled or has no fresh heartbeat.

An optional module may remain `SKIPPED` and can be configured later. For
example, a deployment can skip `user_sso` and use only the built-in Agent
Bearer Token capability. Disabling an active optional module follows
dependency-aware safe-disable rules; it is not equivalent to deleting its
stored configuration.

## Interactive terminal workflow

Use the module list to resume one item at a time:

```bash
pas config modules
pas config configure user_sso
pas config skip user_sso
pas config show runtime_policy
```

Each edit creates a draft. Validation checks syntax, dependencies, and external
connectivity without changing the effective runtime configuration. Activation
requires a fresh validation proof and an expected revision, so concurrent
administrators cannot silently overwrite one another.

## Declarative workflow and dry run

Secret indirection uses the general `<field>_from_env` convention. The CLI
reads the named environment variable locally and sends the secret over the
authenticated connection; the YAML never contains plaintext.

```yaml
protocol_version: 1
core_admin:
  desired_state: active
  config:
    username: admin
    password_from_env: PAS_SETUP_ADMIN_PASSWORD
user_sso:
  desired_state: skipped
aliyun_access:
  desired_state: active
  config:
    credential_mode: assume_role
    region_id: cn-hangzhou
    openapi_network: public
    assume_role:
      source_access_key_id_from_env: PAS_STS_SOURCE_ACCESS_KEY_ID
      source_access_key_secret_from_env: PAS_STS_SOURCE_ACCESS_KEY_SECRET
      role_arn: acs:ram::<account-id>:role/pas-runtime
      role_session_name: polardb-agentic-a1b2c3d4
      external_id_from_env: PAS_STS_EXTERNAL_ID
```

For `direct_ak`, put `access_key_id_from_env` and
`access_key_secret_from_env` in a `direct_ak` block. For `ecs_ram_role`, use
an `ecs_ram_role` block and, optionally, `role_name`; it never accepts an
AccessKey. Every secret value in a declaration uses `_from_env` (or the
supported `_from_file` or `_from_stdin` equivalent), never plaintext YAML.
PAS stores fixed credentials encrypted and keeps temporary credentials only
in process memory; it never exports them to Agents, sandboxes, browsers, or
the metadata database.

Set `openapi_network` to `vpc` when the service Pod has Alibaba Cloud VPC
connectivity but no Internet egress. Both PolarDB and STS then use their
region-specific VPC endpoints. The default is `public`; custom endpoint
hostnames are intentionally rejected.

Dry run and validation originate from the PAS backend Pod, so that Pod must
resolve and route to the selected endpoints. The backend performs a read-only
PolarDB metadata request and, for AssumeRole, obtains STS credentials first.
The UI shows the exact resolved endpoints and retains only these stable,
sanitized failure codes:

- `OPENAPI_DNS_FAILURE`
- `OPENAPI_CONNECT_FAILURE`
- `OPENAPI_TLS_FAILURE`
- `OPENAPI_ENDPOINT_UNSUPPORTED`
- `OPENAPI_CREDENTIAL_INVALID`
- `OPENAPI_PERMISSION_DENIED`
- `OPENAPI_STS_SOURCE_CREDENTIAL_INVALID`
- `OPENAPI_STS_ASSUME_ROLE_DENIED`
- `OPENAPI_STS_ROLE_TRUST_REJECTED`
- `OPENAPI_STS_EXTERNAL_ID_MISMATCH`
- `OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED`
- `OPENAPI_ECS_METADATA_DISABLED`
- `OPENAPI_ECS_IMDSV2_UNAVAILABLE`
- `OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED`

Raw SDK exceptions and configured credentials are never returned to the
browser. See [troubleshooting](../operations/troubleshooting.md) for the
mode-specific corrective action for each stable code.

Always inspect a plan before applying it:

```bash
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
```

Dry run performs parsing, normalization, schema checks, dependency planning,
and non-mutating validation. It does not save drafts, activate modules, consume
the bootstrap token, or create cloud resources.

## Alibaba Cloud credential modes

Choose one credential mode for the active `aliyun_access` configuration:

- `direct_ak` uses a long-lived AccessKey pair and is mainly a controlled
  development, migration, or recovery option.
- `assume_role` stores only a dedicated low-privilege source AccessKey in PAS,
  then assumes the RAM role and obtains a renewable temporary identity
  credential (STS Token). An optional External ID is secret input, not a
  display value.
- `ecs_ram_role` stores no AccessKey. PAS must be deployed on an ECS instance
  authorized with the RAM role. The runtime uses IMDSv2 only; there is no
  IMDSv1 fallback or user-configured metadata URL.

Use **Test connection** before **Save and enable** when connectivity is
available. A successful dry run is recommended, not required: saving after a
failed or unavailable validation requires an explicit confirmation. The result
shows only the selected mode, endpoint decision, masked identity or role,
temporary expiration when relevant, a stable error code, and Alibaba Cloud
Request ID.

## Export, backup, and recovery

Export returns the effective configuration with secrets represented only as
configured/redacted markers:

```bash
pas config export --file effective.yaml
pas config export --module runtime_policy --file runtime-policy.yaml
```

Use exports for review and environment templates, not as a secret backup.
Back up the metadata database and root key separately. A restore requires both.
Rotating the root key is an explicit, audited re-encryption operation; changing
the Secret value alone is not rotation.

## External URLs and reload behavior

The setup UI uses same-origin requests by default. An Agent Token-only
deployment on a controlled private network may use an HTTP
`runtime_policy.external_base_url`; the Agent MCP endpoint remains available
after restart, but interactive MCP OAuth metadata is intentionally not
advertised at that insecure origin. Configure a trusted, externally reachable
HTTPS origin before enabling OAuth or OIDC. The service does not infer the
origin from untrusted proxy headers.

Active configuration is projected into immutable in-process snapshots. Every
replica polls the global version every 5 seconds by default (allowed range
1–60 seconds), reloads changed modules in dependency order, and keeps the last
known-good snapshot if a required adapter fails.

All safe runtime services are started while the installation is in `SETUP`
mode, but the runtime access policy blocks business endpoints. Activating
`core_admin` therefore changes every replica to `READY` through the same
version-polling path; a separate restart is not required.

`GET /readyz` compares the global database version with the version loaded by
that replica. It reports `desired_config_version`, `loaded_config_version`,
`config_status`, `last_reload_error`, and `module_errors`. A stale replica or a
required reload failure returns HTTP 503, allowing a Kubernetes readiness probe
to remove it from Service traffic until it converges. Optional adapter failures
report `DEGRADED` while the replica retains the previous effective module.

Use `/readyz` as the Kubernetes readiness probe. With the default interval,
normal propagation takes no more than approximately 5 seconds plus database
latency. Readiness is evaluated independently by every Pod, so traffic is not
sent to a replica merely because another Pod has already loaded the new
version.

## Operational checks

Use `pas config modules` to inspect module state and revision. A draft never
changes effective behavior until validated and activated. Secret response
fields remain redacted, and exported configuration cannot be used to recover
secret plaintext.

After configuration, continue with the
[database instance access and provisioning guide](../database-instances/access-and-provisioning.md).
