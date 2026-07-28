# Database instance access and provisioning

**English** | [简体中文](../../zh-cn/database-instances/access-and-provisioning.md)

For focused procedures, see [instance registration](registration.md) and
[multitenant provisioning](multitenant-provisioning.md).

This guide explains how an administrator registers PolarDB MySQL instances,
manages credentials and provisioning backends in the web console, grants
Users and Agents access, and operates persistent logical databases through
MCP.

## Scope

This release supports physical instances with engine `polardb_mysql`,
topology `single_tenant` or `multitenant`, and allocation mode `registered`.
Both ordinary and multitenant registered instances can be exposed through
direct bindings. Only `polardb_mysql` + `multitenant` instances can be
provisioning backends for `create_db_instance`.

A production Agent can have direct bindings to multiple physical instances.
It uses `list_db_instances` to discover the instances and capabilities it may
use, `describe_db_instance` to retrieve authorized connection details, and
`run_sql` to send SQL through the MCP service's SQL-over-HTTP proxy. The
service connects to the selected backend with the registered or bound MySQL
account.

Provisioned logical databases are persistent until an explicit
`delete_db_instance` call. This release does not create dedicated PolarDB
clusters and does not automatically delete resources after a fixed lifetime.

Before enabling multitenant provisioning, review the
[official PolarDB for MySQL multitenant management documentation](https://help.aliyun.com/zh/polardb/polardb-for-mysql/user-guide/multi-tenant-management-instructions)
for prerequisites, supported configurations, resource isolation, and
tenant-management SQL.

## Before you begin

The MCP server, each Agent workload, and each registered PolarDB endpoint must
have VPC connectivity. In production:

- Publish the MCP server through an internal HTTPS ingress or load balancer.
- Set a stable `PAS_ENCRYPTION_KEY`; it protects Agent Token ciphertext and
  database credentials at rest.
- Configure persistent JWT signing keys.
- Use MySQL or PostgreSQL for the metadata database in multi-replica
  deployments. SQLite is for single-process functional development only.
- Keep every direct-access account least-privileged and never expose a
  `provisioning_admin` account to an Agent.

Run migrations before starting the service:

```bash
uv run alembic upgrade head
uv run python -m server
```

Physical instances, credentials, provisioning backends, and Agent bindings
are stored in the metadata database and managed in the UI. There is no
single-instance environment setting, and changes do not require service
redeployment.

## Register physical instances

Sign in to the web console as an active administrator and open
**Instances → Register Instance**:

1. Enter the PolarDB cluster identifier, a recognizable display name, and an
   optional **Usage** description. Usage is trimmed, limited to 1024
   characters, and helps authorized Agents understand the instance's purpose.
2. Select engine `polardb_mysql`.
3. Select topology `single_tenant` for an ordinary instance or
   `multitenant` for an instance with PolarDB multitenant management enabled.
4. Enter the optional region and the required VPC host, port, username, and
   password. The MySQL port defaults to `3306`. The allocation mode is fixed
   to `registered` and is not shown in the form.
5. Choose **Test Connection**. The service opens a temporary connection and
   executes `SELECT 1`. For topology `multitenant`, it also requires
   `enable_multi_tenant=ON` and verifies that the submitted username is an
   exact member of the comma-separated `rds_kill_user_list`. The connection
   is closed without storing the submitted password.
6. Choose **Register Instance**. The service repeats the connectivity and
   topology-specific checks, then atomically stores the instance and
   encrypted credential. A failed check leaves neither record behind.

Registration records an existing physical instance; it does not create a
PolarDB cluster. A single-tenant registration creates a `direct_access`
credential; a multitenant registration creates a `provisioning_admin`
credential. Open the instance detail page to review usage,
credentials, bindings, and provisioning state. Choose **Edit Instance** to
change its display name, Usage, region, host, or port. Cluster ID, engine,
topology, and allocation mode remain immutable. Changing Host or Port requires
an active credential and repeats the connection test from the PAS backend Pod
before saving. Editing metadata alone does not require a connection test.
Removing an unreferenced registered instance also removes its owned
credentials. Removal is rejected while bindings or a provisioning backend
remain.

The instance inventory **Provisioning** column describes automatic database
provisioning, not general physical-instance health. `Not enabled` means no
Provisioning Backend is configured. `Healthy` or `Unhealthy` reports the
latest live provisioning connection check.

MCP forwards SQL using the selected MySQL account. The databases, tables, and
operations the Agent can use are ultimately constrained by that account's
MySQL grants. The MCP service does not bypass or elevate backend permissions;
use a least-privileged account whose grants match the intended Agent access.

Multitenant registration can return these additional preflight errors:

- `MULTITENANT_DISABLED`: contact PolarDB support to enable
  `enable_multi_tenant`, then restart the cluster before retrying.
- `MULTITENANT_ADMIN_REQUIRED`: use a supported high-privilege account whose
  exact username appears in `rds_kill_user_list`.
- `MULTITENANT_PREFLIGHT_FAILED`: the service could not read a required
  PolarDB variable or received an invalid result. Verify cluster
  compatibility and the account's ability to inspect server variables.

To associate a Department with multitenant capacity, open **Departments**,
expand the Department, and choose **Bind Instance**. Select an active
registered multitenant instance from the list. Connection details and
credentials are never entered in Departments; all instance registration is
owned by **Instances**. A Department can have at most one multitenant
instance, while one multitenant instance may serve multiple Departments.

## Configure credentials and a backend

Credentials are separate records with separate purposes:

- A `direct_access` credential is the ordinary database account returned only
  through an authorized direct binding. Its declared capability is
  `readonly` or `readwrite`.
- A `provisioning_admin` credential must have capability `admin`. It is valid
  only for a `polardb_mysql` + `multitenant` instance and is used internally
  for tenant, account, database, grant, verification, and cleanup operations.
  It is never returned by an MCP Tool.

Registration creates the topology-appropriate initial credential. A
multitenant instance may also have `direct_access` accounts for ordinary SQL;
they remain separate from its high-privilege `provisioning_admin` account.
Choose **Test Connection** before adding or editing a credential. PAS repeats
the test server-side before committing. A scoped Direct Access credential
connects to its declared database and executes `SELECT 1`; a Provisioning
Administrator also passes the multitenant prerequisite checks.

Choose **Edit** to change an active credential or rotate its password without
changing its ID or existing bindings. Leave Username or Password empty to
retain the stored value. A successful update increments the credential
version, causing stale connection pools to reconnect on their next use.
Plaintext is encrypted at rest. An administrator may reveal a
credential only after explicit confirmation; Reveal is audited, rate-limited,
and returned with `Cache-Control: no-store`. Revocation immediately makes the
credential unusable for new access.

For a multitenant instance, choose **Configure Provisioning Backend** and
select the `provisioning_admin` credential created during registration. Set
priority, maximum active resources, resource CPU range, and DDL concurrency.
The server validates the definition and connection before activation.

Backend states are:

- `active`: eligible for new resources and background processing.
- `draining`: receives no new resources; existing work and cleanup continue.
- `disabled`: forward provisioning stops; cleanup and recovery remain
  available.

Multiple registered multitenant instances can be configured as independent
backends. Selection considers authorization, engine, health, priority, and
capacity.

## Create an Agent and manage its Token

Open **Agents → Create Agent**. Set a unique name and, when needed, a maximum
active-resource limit. Agent and Token have a one-to-one relationship; each
Agent has exactly one Token record:

- The creation response displays the new `pas_agent_...` Token.
- The Agent detail page automatically displays the current active plaintext
  Token to an authenticated administrator.
- **MCP server URL** uses the active Runtime Policy `external_base_url` with
  `/mcp`. If that setting is empty or unavailable, it falls back to the
  console origin. Configure an address that the intended MCP client can
  reach; a VPC address requires the client to have VPC connectivity.
- **Copy JSON configuration** copies the MCP URL and Token in a ready-to-paste
  client configuration whose server name is the Agent name.
- **Regenerate Token** replaces it; the previous Token stops authenticating
  immediately.
- **Revoke Token** stops authentication until a new Token is generated.

The copied configuration has this shape:

```json
{
  "mcpServers": {
    "<agent-name>": {
      "url": "https://console.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <agent-token>"
      }
    }
  }
}
```

When `expires_at` is absent, the Token remains valid until regeneration or
revocation. If an expiry is configured, the Token cannot authenticate or be
displayed after that time. The web console reports it as expired; an
administrator must use **Regenerate Token** to issue a new active Token.

The server stores a SHA-256 hash for authentication and encrypted ciphertext
for administrator display. Loading the active plaintext is audited and
rate-limited; secret responses use `Cache-Control: no-store`, and the console
keeps the value only in React memory. Do not copy a Token into URLs, logs,
analytics, browser storage, or source control. Store it in a secret manager
and send it only as:

```http
Authorization: Bearer <agent-token>
```

Audit records are retained for 180 days by default. The cleanup worker removes
at most 500 oldest expired records once per hour. Operators can tune
`sql_security.audit.retention_days`, `cleanup_interval_seconds`, and
`cleanup_batch_size`; setting the interval to `0` disables scheduled cleanup.

Disabling an Agent also denies authentication and effective instance access.

## Bind instance access

Agent detail uses one **Instance access** editor per registered physical
instance. A single save atomically updates the direct-access and provisioning
parts; a partial failure leaves both unchanged. An instance that already has
access is omitted from the new-access picker, including when one underlying
part is disabled. Edit or remove the existing row instead of creating a
duplicate.

Direct access selects an active `direct_access` credential belonging to the
instance, a `readonly` or `readwrite` permission, and any of:

- `db_instance:list`
- `db_instance:describe`
- `db_instance:credentials:read`

The first direct credential selection enables **Enable SQL over HTTP proxy**
by default, exposing `run_sql`, `run_sql_transaction`, and
`describe_schema`. When selected, `readonly` stores `sql:read`, while
`readwrite` stores both `sql:read` and `sql:write`. Clear the option for
inventory- or metadata-only access. Changing permission preserves the proxy
selection and recalculates its SQL capabilities.

For an active registered `polardb_mysql` + `multitenant` instance, the same
editor also shows **Create managed databases**. It is cleared by default and
adds `db_instance:create` only when explicitly selected. Selection is allowed
only after the instance has an `active`, freshly healthy provisioning backend;
otherwise the editor links to the instance page where the administrator can
configure or repair the backend.

Provisioning-only access is valid: an administrator may select only
**Create managed databases**, without choosing a direct credential or SQL
permission. Conversely, direct-only access does not grant creation. To combine
both on a multitenant instance, first add a separate `direct_access`
credential on the instance because its registration credential is a
non-exposable `provisioning_admin` account.

Clearing **Create managed databases** prevents new `create_db_instance`
requests while leaving existing Agent-owned logical databases available to
list, describe, and delete. Removing the whole instance-access row is rejected
while any non-deleted owned resource remains. The UI then offers to disable
managed database creation; delete the owned resources before removing the
aggregate access row.

Capability dependencies are expanded: credential read implies describe and
list, and describe implies list. The requested permission cannot exceed the
declared credential capability. The server uses the intersection of the
binding, capability set, credential status, Agent status, and resource
ownership on every call.

A `readonly` selection does not rewrite grants on a write-capable database
account. For database-enforced read-only access, bind an account whose PolarDB
grants are actually read-only. MCP policy is an additional guard; the MySQL
backend remains the authority for databases, objects, and SQL privileges.

Under **Users**, administrators can separately select an instance and edit
the User's credential, permission, capabilities, and enabled state. Existing
system-created SQL access stays SQL-only unless an administrator explicitly
grants instance Tool capabilities and a valid direct credential.

## Tool authorization and credential rules

Tool visibility is derived from current access:

- `list_db_instances` appears with list capability or an owned non-deleted
  resource.
- `describe_db_instance` appears with describe capability or an owned
  non-deleted resource.
- `run_sql`, `run_sql_transaction`, and `describe_schema` appear for an Agent
  with either an enabled direct-access part whose credential and `sql:read`
  capability remain valid, or an owned `READY` provisioned resource with a
  valid resource credential. Clearing **Enable SQL over HTTP proxy** removes
  the SQL capabilities from that physical instance access. `sql:write`
  controls which statements may execute; it does not add another Tool.
- An active Agent with `db_instance:create` on an instance whose provisioning
  backend is active, fresh, and healthy is
  offered all four database instance Tools up front, including
  `create_db_instance` and `delete_db_instance`.
- A resource owner without current creation access is offered list,
  describe, and delete while it owns a non-deleted resource. A usable
  `READY` resource also makes the three SQL Tools available.

Treat the returned catalog as a stable discovery hint, not a promise that an
operation will succeed. Capacity does not remove `create_db_instance` from
the catalog; a saturated backend can instead return `CAPACITY_EXHAUSTED`.
Every Tool call reauthorizes the applicable current binding, backend,
ownership, health, and capacity state. Many MCP clients cache their first
`tools/list`; this release does not rely on
`notifications/tools/list_changed`. Reconnect or call `tools/list` again after
an administrator changes bindings, backend state, Agent status, or resource
state.

`list_db_instances` returns safe metadata, permission, capability names, and
the `usage` key. `describe_db_instance` returns the same usage metadata.
Registered physical instances expose the administrator-provided value;
unspecified physical instances and provisioned logical resources return
`null`. The list omits `DELETED` resources by default. Describe includes direct
credentials only when `db_instance:credentials:read` is effective and the
credential is still valid. Provisioned credentials and the `run_sql_read`
and `run_sql_write` capabilities appear only while the owned resource is
`READY` and its resource credential is usable. Lifecycle records in
`CREATING`, `FAILED`, `DELETING`, or `DELETE_FAILED` remain visible but
cannot execute SQL. Missing authorization is reported like a missing instance
to resist identifier enumeration.

For a registered physical instance, `db_instance_id` is the stable, opaque
UUID of its metadata-database registration. An owned provisioned resource
uses its stable `dbi-*` resource ID. Neither is a binding ID, and neither is
regenerated on each list request. `name` is display-only, can change, and is
not required to be unique.

For an Agent, every `ACTIVE` or `READY` entry returned by
`list_db_instances` that contains `run_sql_read` can be passed unchanged to
`run_sql`, `run_sql_transaction`, and `describe_schema`. Provisioned
resources always execute with their generated account and database. The
`database` argument must be omitted or equal that resource database; it
cannot select another database on the backing multitenant instance.

Agents must always provide `instance_id` for SQL Tools. They cannot use
`set_default_instance` or the Branch Tools. Each invocation revalidates the
specific binding or resource ownership, status, capability, and credential.

## MCP calls

The endpoint uses JSON-RPC over Streamable HTTP:

```text
POST /mcp
Content-Type: application/json
Accept: application/json, text/event-stream
Authorization: Bearer <agent-token>
```

List authorized instances with optional `cursor`, `limit` (1–200), `db_type`,
`source`, and `status` filters:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "list_db_instances",
    "arguments": {
      "limit": 50,
      "db_type": "polardb_mysql"
    }
  }
}
```

Execute SQL against any usable returned instance by reusing its
`db_instance_id` unchanged as `instance_id`:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "run_sql",
    "arguments": {
      "instance_id": "dbi-00000000000000000000000000000000",
      "sql": "SELECT 1"
    }
  }
}
```

`run_sql_transaction` and `describe_schema` use the same explicit
`instance_id`. A `readonly` binding accepts read-only statements and schema
inspection; a `readwrite` binding or `READY` provisioned resource can run
writes subject to the existing SQL safety and confirmation policy. A resource
still being created asks the caller to retry after it becomes `READY`; failed
or deleting resources direct the caller to inspect or choose another listed
resource. In every case, the selected MySQL account's backend grants are the
final database, object, and operation boundary. The service does not bypass
or elevate those grants.

Create a persistent logical database:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "create_db_instance",
    "arguments": {
      "client_token": "production-agent-2026-001",
      "db_type": "polardb_mysql",
      "name": "orders-reporting"
    }
  }
}
```

`client_token` must contain 1–128 ASCII letters, digits, `.`, `_`, `:`, or
`-`. It is permanently bound to the normalized `db_type` and optional `name`
within that Agent. An identical retry returns the same `db_instance_id`,
including after `DELETED`. A different request with the same key returns
`IDEMPOTENCY_CONFLICT`.

Describe an authorized physical instance or owned logical database:

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "describe_db_instance",
    "arguments": {
      "db_instance_id": "dbi-..."
    }
  }
}
```

Honor `retry_after_seconds` after `RATE_LIMITED`. Do not log a response that
contains `host`, `database`, `username`, or `password`.

Delete an owned logical database:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "delete_db_instance",
    "arguments": {
      "db_instance_id": "dbi-..."
    }
  }
}
```

The database instance Tools are exposed through MCP, not through a parallel
public REST lifecycle API.

### Common Tool errors

- `INVALID_CLIENT_TOKEN`: `create_db_instance` received a key with an invalid
  length or character.
- `IDEMPOTENCY_CONFLICT`: the Agent reused a key with different create
  parameters.
- `UNSUPPORTED_DB_TYPE`: a create or list request named an unsupported
  database type.
- `NO_PROVISIONING_BACKEND`: no currently authorized, active, healthy backend
  can accept the Agent's request.
- `CAPACITY_EXHAUSTED`: authoritative reservation found that neither backend
  nor Agent capacity was available.
- `DB_INSTANCE_NOT_FOUND`: describe or delete could not find an instance
  visible to that principal.
- `INVALID_CURSOR`: a list cursor is invalid, expired, or does not match the
  current filters.
- `RATE_LIMITED`: list or describe exceeded its rate limit; honor the returned
  `retry_after_seconds`.

## Resource lifecycle and deletion

```text
create_db_instance(client_token, db_type, name?)
        |
        v
     CREATING ---- provisioning failure ----> FAILED
        |
        v
       READY
        |
delete_db_instance(db_instance_id)
        |
        v
     DELETING ---- cleanup failure ---------> DELETE_FAILED
        |
        v
      DELETED
```

Creation durably reserves capacity and returns `CREATING`; background workers
perform tenant, resource configuration, account, database, grant, and
connection verification. Only `READY` exposes provisioned connection
credentials.

Deletion immediately stops credential disclosure. Cleanup locks the account,
terminates active sessions, removes database objects and tenant resource
configuration, verifies that no residue remains, retires the encrypted
resource credential, and releases capacity. Capacity is released only after
verification. `delete_db_instance` is idempotent for deletable states.

The resource row and its `client_token` remain after `DELETED` as permanent
idempotency and audit history. Deletion is always explicit; no background
age-based sweep changes a persistent resource to `DELETING`.

## Operations and recovery

Health and dispatch workers read backend configuration from the metadata
database. Another process can resume work after a worker claim is no longer
active. Provisioning and cleanup failures use bounded retries and preserve
their last completed step.

For a backend incident:

1. Set it to `draining` to stop new placement while existing work continues.
2. Resolve network, credential, health, or capacity issues.
3. Reactivate it after validation, or set it to `disabled` while retaining
   cleanup and recovery paths.
4. Inspect sanitized server logs and resource steps for `FAILED` or
   `DELETE_FAILED`; never copy secret-bearing responses into tickets.

Do not revoke a backend's `provisioning_admin` credential while resources
still need cleanup. Aggregate Agent instance access cannot be removed while
the Agent owns non-deleted resources on that backend; clear
**Create managed databases** instead, then delete the resources before
removing the access row.

## Functional acceptance

Run focused lifecycle, authorization, and UI-administration contract tests:

```bash
PAS_DATABASE_URL=sqlite+aiosqlite:///:memory: uv run --extra dev pytest \
  tests/test_db_instance_e2e.py \
  tests/test_dynamic_tool_catalog.py \
  tests/test_agent_binding_admin_api.py \
  tests/test_provisioning_backend_admin_api.py -q
```

Run the complete backend suite and frontend checks:

```bash
PAS_DATABASE_URL=sqlite+aiosqlite:///:memory: uv run --extra dev pytest -q

cd web
npm test -- --run
npm run lint
npm run build
```

Local tests use simulated database operations and are not evidence of VPC
connectivity or real PolarDB multitenant DDL. Use a controlled VPC environment
for integration and performance acceptance.
