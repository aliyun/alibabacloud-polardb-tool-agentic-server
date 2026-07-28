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
Administrators can open `/settings/configuration` in the console to review or
change modules. Visiting `/setup` after ownership is established redirects to
that authenticated page. You may also use the interactive and declarative CLI
commands below.

## Modules and dependencies

`core_admin` and `token_security` establish the administrator and shared JWT
key ring. `runtime_policy`, `sql_security`, and `observability` begin with
materialized safe defaults.

The remaining capabilities are modular:

- `agent_token_auth` enables administrator-issued Agent Tokens.
- `user_sso` enables human OIDC sign-in and may remain `SKIPPED`.
- `aliyun_access` owns encrypted Alibaba Cloud credentials, the region,
  and `openapi_network` (`public` or `vpc`) selecting the reviewed
  PolarDB and STS OpenAPI endpoint families.
- `agentic_db_purchase` depends on `aliyun_access` and owns the PolarDB
  cluster purchase specification (engine version, node class, proxy,
  serverless scaling, and storage) shared by pooled and dedicated
  cluster creation.
- `resource_pool` depends on `agentic_db_purchase` and owns network
  placement plus pool sizing and replenishment behavior. `region_id`
  and `zone_id` are required. `vpc_id` and `vswitch_id` are optional;
  when omitted, Alibaba Cloud creates clusters in the account's
  default VPC.

An optional module may remain `SKIPPED` and can be configured later. For
example, a deployment can skip `user_sso` and use only administrator-issued
Agent Tokens. Disabling an active module follows dependency-aware safe-disable
rules; it is not equivalent to deleting its stored configuration.

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
    credential_mode: direct_ak
    access_key_id_from_env: ALIBABA_CLOUD_ACCESS_KEY_ID
    access_key_secret_from_env: ALIBABA_CLOUD_ACCESS_KEY_SECRET
    region_id: cn-hangzhou
    openapi_network: public
```

Set `openapi_network` to `vpc` when the service Pod has Alibaba Cloud VPC
connectivity but no Internet egress. Both PolarDB and STS then use their
region-specific VPC endpoints. The default is `public`; custom endpoint
hostnames are intentionally rejected.

Dry run and validation originate from the PAS backend Pod, so that Pod must
resolve and route to the selected endpoints. The backend performs a read-only
PolarDB metadata request and, for AssumeRole, obtains STS credentials first.
The UI shows the exact resolved endpoints and retains only sanitized failure
categories:

- `OPENAPI_DNS_FAILURE`
- `OPENAPI_CONNECT_FAILURE`
- `OPENAPI_TLS_FAILURE`
- `OPENAPI_ENDPOINT_UNSUPPORTED`
- `OPENAPI_CREDENTIAL_INVALID`
- `OPENAPI_PERMISSION_DENIED`

Raw SDK exceptions and configured credentials are never returned to the
browser.

Always inspect a plan before applying it:

```bash
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
```

Dry run performs parsing, normalization, schema checks, dependency planning,
and non-mutating validation. It does not save drafts, activate modules, consume
the bootstrap token, or create cloud resources.

## Export, backup, and recovery

Export returns the effective configuration with secrets represented only as
configured/redacted markers:

```bash
pas config export --file effective.yaml
pas config export --module resource_pool --file resource-pool.yaml
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
