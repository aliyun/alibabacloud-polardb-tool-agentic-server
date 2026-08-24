# Alibaba Cloud PolarDB Tool Agentic Server

**English** | [简体中文](README_zh-CN.md)

An open-source Model Context Protocol (MCP) gateway for PolarDB for MySQL. It
gives people and independently managed Agents authenticated, auditable access
to database discovery, controlled SQL operations, branch operations, and
persistent logical database resources.

## Features

- Streamable HTTP MCP endpoint with OAuth and built-in authentication.
- PolarDB instance discovery, routing, and capability-based access control.
- Guarded SQL execution for Users, with row limits, confirmation, rate
  limiting, and audit records.
- Independent Agent identities with one-to-one API Tokens managed in the web
  console.
- Direct access to multiple registered PolarDB MySQL instances.
- Database-backed multitenant provisioning backends with health, capacity,
  draining, cleanup, and recovery controls.
- Auto-provisioning pools with typed AgenticDB Dedicated purchasing, prewarming, pool-owned cold
  creation, Agent primary/fallback routing, cooldown, restore, and reclamation.
- Four authorization-aware instance Tools: `list_db_instances`,
  `create_db_instance`, `describe_db_instance`, and `delete_db_instance`.
- FastAPI backend and React/Vite administration console.

## Quick start

### Prerequisites

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 or later, only when developing the web console

### Start the backend

```bash
uv sync --extra dev

mkdir -p data
if [ -s data/polardb_agentic.db ] && [ ! -f data/pas-root-key ]; then
  echo 'Existing PAS database requires its original data/pas-root-key' >&2
  exit 1
fi
if [ ! -f data/pas-root-key ]; then
  (umask 077; python3 -c \
    'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' \
    > data/pas-root-key)
fi
chmod 600 data/pas-root-key

export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="file:$PWD/data/pas-root-key"

uv run pas database migrate
uv run pas database check
uv run pas serve
```

The backend listens on `http://localhost:18760`. `PAS_DATABASE_URL` and
`PAS_ENCRYPTION_KEY` are the only server bootstrap settings. In production,
provide the root key through a Kubernetes Secret or restricted mounted file,
back it up separately, and use a durable MySQL or PostgreSQL metadata database.
Never generate a replacement key for an existing database. The database check
fails closed when the configured key cannot decrypt persisted configuration.

### Start the web console

```bash
cd web
npm install
npm run dev
```

Open `http://localhost:18761`. On an empty database, the setup console asks for
the one-time bootstrap token and guides creation of the first administrator.
Optional modules such as SSO and Alibaba Cloud access may be skipped and
configured later. Auto-provisioning pools are managed separately on the
**Pool** page.

Terminal-only deployments can use the interactive or declarative workflow:

```bash
pas config init
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
pas config export --file effective.yaml
```

See [initial setup](docs/en/setup/initial-setup.md) for bootstrap-token
delivery, Docker and Kubernetes commands, and recovery. Continue with
[guided modular configuration](docs/en/configuration/guided-configuration.md)
for module dependencies, secret indirection, and configuration workflows.

For a production-like single-host trial, use the supported
[Docker Compose deployment](docs/en/deployment/docker-compose.md). It starts
pinned MySQL 8.0, runs migrations once, and then starts the server. Kubernetes
operators should begin with the
[production prerequisites](docs/en/deployment/prerequisites.md) and the
[Helm deployment guide](docs/en/deployment/kubernetes-helm.md).

Codex, Claude Code, Cursor, and compatible Agent Skills clients can use the
explicitly invoked [agent-assisted deployment skill](docs/en/deployment/agent-assisted-deployment.md).
It validates a Linux target before mutation and pins PAS to release `0.0.9`.

## Administration workflow

The web console is the source of truth for runtime instance access:

1. Under **Instances**, register a `polardb_mysql` physical instance with
   topology `single_tenant` or `multitenant`, and provide its reachable host,
   port, username, and password. Optionally add a Usage description so Agents
   can identify the instance's purpose. Use **Test Connection** to execute
   `SELECT 1` before registration. Allocation mode is fixed to `registered`.
   After registration, use **Edit Instance** on the detail page to update its
   display name, Usage, region, host, or port.
2. Registration creates the initial encrypted `direct_access` credential, or
   `provisioning_admin` credential for a `multitenant` instance. Configure its
   provisioning backend, capacity, CPU range, and DDL concurrency when
   multitenant logical database creation is needed.
3. Under **Agents**, create an Agent and securely store its Token. An
   administrator sees the active Token and MCP client configuration, and can
   regenerate or revoke it. Regeneration invalidates the previous Token
   immediately.
4. Under the Agent's unified **Instance access**, grant only the required
   direct metadata or SQL capabilities. On an active, healthy multitenant
   provisioning backend, optionally select **Create managed databases**;
   creation is off by default and can be granted without direct SQL access.
5. Under **Users**, administrators can also edit each User's per-instance
   credential, `readonly` or `readwrite` permission, and capabilities.
6. Under **Pool**, create one or more auto-provisioning pools. Configure target and
   hard capacity, network placement, permissions, reclaim policy, and purchase
   budgets; then bind one primary pool and optional fallback pools to each
   Agent.

Human Users use only assigned registered instances and never trigger physical
cluster purchases. Auto-provisioning cold creation first creates a member inside the
selected pool, so every PAS-purchased cluster remains pool-managed.

Provisioning backends and their credentials are stored in the metadata
database. No deployment-time environment variable selects a single
multitenant instance, and changing a binding does not require redeployment.

## Database instance Tools

The authorized Tool catalog changes with the authenticated principal's
bindings and owned resources:

- `list_db_instances` lists authorized physical instances and non-deleted
  resources, with cursor pagination, filters, and their optional `usage`
  description.
- `create_db_instance(client_token, db_type, name?, provisioning_mode?)` is
  Agent-only and currently accepts `db_type="polardb_mysql"`. It creates a
  persistent database through an authorized `multitenant` or `dedicated`
  backend. Omitted mode remains compatible with `multitenant` and emits a
  deprecation metric; new callers should always send it explicitly.
- `describe_db_instance(db_instance_id)` returns authorized metadata and only
  includes connection credentials when the caller has credential-read access
  and the resource is ready. Its `usage` field matches the registered physical
  instance; unspecified and provisioned resources return `null`.
- `delete_db_instance(db_instance_id)` is Agent-only and deletes an owned,
  provisioned resource.
- A valid direct binding exposes `run_sql`, `run_sql_transaction`, and
  `describe_schema`. Agents must pass the stable instance UUID returned by
  `list_db_instances` as `instance_id`; display names are not identifiers.

`client_token` is a permanent idempotency key within one Agent. Reusing it with
the same normalized request returns the original resource, including after
`DELETED`; using it for different parameters returns an idempotency conflict.
Resources have no automatic lifetime in this release—call
`delete_db_instance` explicitly.

Agents can use the same lifecycle through the Bearer-only REST endpoints at
`/mcp/rest/db-instances`; their canonical schema is
`/mcp/rest/openapi.json`. Dedicated delete first disconnects access, then uses
the configured cooldown before destroy or sanitize/reuse.

See the [database instance access and provisioning guide](docs/en/database-instances/access-and-provisioning.md)
for the complete UI workflow, security model, Tool examples, and lifecycle.

## Documentation

- [English documentation](docs/en/README.md)
- [简体中文文档](docs/zh-cn/README.md)
- [Getting started tutorial](docs/en/getting-started/overview.md)
- [Initial setup](docs/en/setup/initial-setup.md)
- [Guided modular configuration](docs/en/configuration/guided-configuration.md)
- [Database instance access and provisioning](docs/en/database-instances/access-and-provisioning.md)
- [Dedicated database hot pools](docs/en/database-instances/dedicated-hot-pools.md)
- [Agent REST database provisioning](docs/en/database-instances/agent-rest-provisioning.md)
- [PolarRAG administrator onboarding](docs/en/knowledge/polarrag-onboarding.md)
- [PolarRAG MCP and enterprise identities](docs/en/knowledge/polarrag-mcp.md)
- [Enterprise identity sources](docs/en/administration/enterprise-identity-sources.md)
- [Enterprise identity source administrator API](docs/en/reference/enterprise-identity-sources-api.md)
- [Docker Compose deployment](docs/en/deployment/docker-compose.md)
- [Agent-assisted single-host deployment](docs/en/deployment/agent-assisted-deployment.md)
- [Kubernetes and Helm deployment](docs/en/deployment/kubernetes-helm.md)
- [Contributing and translation guide](CONTRIBUTING.md)
- [Example environment variables](.env.example)

## Development checks

```bash
uv run --extra dev ruff check .
PAS_DATABASE_URL=sqlite+aiosqlite:///:memory: \
PAS_ENCRYPTION_KEY=MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE= \
uv run --extra dev pytest

cd web
npm test -- --run
npm run lint
npm run build
```

Performance tests run only when their `PAS_PERF_*` variables identify an
explicit VPC deployment backed by MySQL or PostgreSQL metadata storage.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before
opening a pull request. English is the canonical technical source; update the
corresponding Simplified Chinese page in the same pull request.

## License

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for
attribution information.
