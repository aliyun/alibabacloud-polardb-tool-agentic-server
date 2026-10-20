# Native database and optional acceptance regression

[简体中文](local-regression.zh-CN.md)

This development helper runs the real database migration and concurrency tests
without a Docker engine. Install MySQL server/client and PostgreSQL binaries
on `PATH`: `mysqld`, `mysql`, `initdb`, `pg_ctl`, and `psql`. Run as an ordinary
user; these databases must not be started as root.

From the repository root:

```bash
uv sync --locked --extra dev
uv run python scripts/dev/run_local_regression.py --output-dir /tmp/pas-native-regression
```

The helper starts private MySQL/PostgreSQL processes on random loopback ports,
initializes their own temporary data directories, and creates random databases
named `pas_local_reg_<32 hex digits>`. It accepts no existing database URL.
Inherited PAS, cloud credential and proxy settings are removed. MySQL's
passwordless root and PostgreSQL's trust authentication apply only to these
disposable local servers. Use a trusted development machine.

The matrix migrates and checks SQLite, MySQL and PostgreSQL, then exercises
directory cleanup, configuration/knowledge persistence, MySQL account writes,
PostgreSQL credential/session races and provisioning-capacity row locks.
The native MySQL CLI companion verifies environment generation through a real
connection, password round-trip, file permissions and absence of password echo.
It does not replace acceptance of the packaged container image.
External-backend schema fixtures copy model metadata before DDL, preserving
the source ORM constraints for subsequent SQLite tests in the same process.

To run the complete backend suite on fresh private databases as well:

```bash
uv run python scripts/dev/run_local_regression.py --full --output-dir /tmp/pas-native-full
```

Each run writes command logs, database versions, exit codes and cleanup status.
`--full` also writes `backend-full.xml` and records remaining skips in
`local-regression.json`. A failing command stops the run and retains logs;
normal teardown stops only the helper's own servers and removes their data.
Force-killing the helper can prevent cleanup. Logs and reports belong outside
the repository; do not commit them.

## Compose configuration checks

The two Compose parsing tests require a real Docker Compose plugin or
standalone `docker-compose` executable, but no Docker engine. To use an
explicit standalone binary, verify it against its official release checksum
and pass its absolute executable path:

```bash
PAS_TEST_COMPOSE_BINARY=/absolute/path/to/docker-compose \
  uv run python scripts/dev/run_local_regression.py --full --output-dir /tmp/pas-native-full
```

An invalid explicit binary fails the tests. Without a detected parser, those
checks remain skipped. No parser implementation is mocked.

## Candidate container and Kubernetes acceptance

Use an owned local Docker engine. Install kind and Helm for Kubernetes checks.
Build the candidate without publishing, then explicitly enable its integration
cases in the full runner:

```bash
docker build -t pas-local:acceptance .
uv run python scripts/dev/run_local_regression.py --full \
  --candidate-image pas-local:acceptance \
  --kubernetes-mysql-image mysql:8.0.44 \
  --output-dir "$HOME/pas-container-acceptance"
```

On a VM-backed Docker engine, the output directory must be shared with the VM:
the container case bind-mounts its own temporary output subdirectory. It runs
the actual candidate with a read-only filesystem and the caller's UID/GID,
connects to a private MySQL container, and checks password round-trip, `0600`
permissions and absence of terminal echo. Its containers and network use
random names and are removed on exit. An explicit `PAS_TEST_MYSQL_IMAGE`
overrides the default pinned MySQL image when running that case directly.

`--candidate-image` alone enables the environment-generator case.
`--kubernetes-mysql-image` also enables the kind/Helm lifecycle case and requires
the candidate. `--kind-image registry.example.com/kindest/node:v1.37.0` selects
a mirrored node image and requires Kubernetes acceptance. These flags require
`--full`; they never enable live-cloud tests or accept an existing cluster.
The kind case creates a private kubeconfig and cluster, validates authenticated
two-replica reads/writes through rollout/failure/retry, then cleans up. See the
[Helm smoke guide](../../docs/en/deployment/kubernetes-helm.md#release-smoke-test-in-restricted-networks).

Record the source revision, Dockerfile, image ID and platform with every run.
A local kind lifecycle test does not establish production networking,
infrastructure-provider behavior or compatibility between two release schemas.

## Stopped maintenance upgrade rehearsal

Rehearse the pinned source release against this checkout on a **new private**
native MySQL server. Supply a local MySQL installation directory, not a server
URL or existing data directory:

```bash
uv run python scripts/dev/run_maintenance_rehearsal.py \
  --source-ref "$SOURCE_REF" \
  --mysql-basedir /path/to/mysql-installation \
  --expected-mysql-version 8.0.13 \
  --chrome --output-dir /tmp/pas-maintenance-acceptance
```

On macOS, `--mysql-arch x86_64` selects an already available Rosetta runtime for
an x86_64 MySQL binary. Set `SOURCE_REF` to a locally available, reviewed source
commit before running the command; there is no default source revision. The
bundled source head must be `2c3d4e5f6a7b` and the target must be the generation-2
CONTRACT release. This is a legacy-schema fixture, not a generic public-release
upgrade runner: do not substitute a release tag with a different schema head.
If the required historical source is unavailable, this scenario cannot run.
Use a fresh output directory for each run.

The helper exports source code and installs its frozen Python environment. With
`--chrome`, it also builds both frontends and uses actual Google Chrome for
login, retained Agent visibility and reload after upgrade, rollback and recovery.
It creates random loopback ports and databases, stops two real source processes,
verifies a paired backup on a separate database, migrates/checks/replays, and
checks authenticated target replicas. A local HTTP front door returns 503 while
the maintenance fence is closed.

Environmental failure cases include a read-only server, a deliberately wrong
key and a limited local account that cannot ALTER a later table. Partially
committed DDL must refuse blind replay, followed by paired restoration to a new
database and successful re-upgrade. Image-only rollback must fail; paired
rollback must preserve source records and decrypt their original tokens, while
post-backup writes disappear as expected. No application response is mocked.

Only sanitized logs, row hashes, physical-schema evidence, backup hashes,
timings and optional screenshots remain. Private servers, exported source,
databases and recovery keys/dumps are removed on exit. This is local metadata
acceptance; production traffic fencing, control-plane image pinning, actual
PolarDB builds and cloud resource reconciliation require their own operational
evidence. See [upgrade and rollback](../../docs/en/deployment/upgrade-and-rollback.md#fully-stopped-contract-maintenance).

## Local MCP load profile

The existing latency case can also run against a fully disposable local
profile on both native MySQL and PostgreSQL:

```bash
uv run python scripts/dev/run_local_mcp_load.py --output-dir /tmp/pas-local-mcp-load
```

This helper starts its own databases, migrates fresh schemas, seeds synthetic
backend capacity and an Agent token, and starts the real PAS MCP HTTP listener
on loopback. It executes the existing 1,000-request, 20-concurrency case for
each database, including Server-Timing assertions and deletion requests, then
checks that all 1,000 rows are `DELETING`. Database and listener cleanup is
automatic on normal exit; `local-load.json` records completion and cleanup.
Each database profile uses a separate child process and event loop, keeping
SSE graceful-shutdown state from a stopped listener out of the next profile.

Only the MCP lifespan runs: provisioning/health workers and the full
application lifespan are deliberately absent. Outbound networking is blocked
with the browser runner's guard. No physical cloud resource is created. The
measured profile is `local-loopback-enqueue-only-no-workers`; its quantiles
describe local request handling, without a production latency threshold.
They do not establish cloud provisioning, deployment throughput or an SLA.

## Live-peer upgrade exception rehearsal

For the fixed image's automatic ordinary init, run
`scripts/dev/run_managed_live_upgrade_rehearsal.py` with the same MySQL/output
options below, plus `--source-head` set to the selected source's manifest head
(`9c1d2e3f4a5b`, `cf4a5b6c7d8e`, or `2c3d4e5f6a7b`) and `--mode automatic`.
Pass the matching local commit with `--source-ref`; no private source is fetched
or selected implicitly. The runner verifies mixed authenticated
read/write, old-image restart refusal, original init/check without maintenance,
and serial B replacement. `--mode external-manual` additionally requires
`--legacy-target-ref` pointing to an available target commit with the original
maintenance guard, the expected target schema and the same dependency lock. It
loads that server and leaves ordinary init retrying during a separate manual migration;
it verifies MAINTENANCE_REQUIRED, migration-lock BUSY and eventual init success
without template mutation. Neither mode executes the deployed Provider engine.
The offline `manual_schema_ephemeral.py` renderer prepares an original-Pod
ephemeral-container subresource request, reusing original credential references
and immutable init image; test the runtime with its inspect mode first.

The local-only runner below keeps the selected source peer process alive during
an explicitly requested emergency migration override, restores ordinary init
migration/check, then upgrades the peer serially. It creates its own private
MySQL server; it never accepts a customer database URL.

```bash
uv run python scripts/dev/run_live_upgrade_rehearsal.py \
  --source-ref "$SOURCE_REF" \
  --mysql-basedir /path/to/mysql-8.0.13 \
  --mysql-arch x86_64 --expected-mysql-version 8.0.13 \
  --chrome --output-dir /tmp/pas-live-upgrade-evidence
```

Use `--mysql-arch` only when that architecture needs explicit execution on the
host. `SOURCE_REF` must resolve locally and have schema head `2c3d4e5f6a7b`.
The resolved source revision is recorded and uses its own frozen dependencies.
`rehearsal.json` records B process survival, request observations, DDL lock and
liveness, online-backup restore/decryption, ordinary-guard refusal, original-init
replay, authenticated mixed/target reads and writes, Chrome, and cleanup.
This proves the bounded process-survival goal, not zero failed requests, old-image
restart/rollback or the deployed Provider task engine. New provider semantics
must remain frozen during the mixed window. The offline `live_upgrade_patch.py`
renderer emits only guarded patch/delete documents; it never operates a cluster.

## External acceptance boundaries

The following remain explicit environment-dependent acceptance:

| Test | Required environment | Local companion and limit |
| --- | --- | --- |
| Live OpenAPI credentials | Authorized cloud account and reachable endpoints | `tests/test_aliyun_openapi_wire.py` uses the real SDK with loopback success/empty/error responses; IAM and cloud routing remain unverified |
| Candidate-image environment generation | Docker engine and candidate image | `--candidate-image` runs the actual image; the native CLI companion alone does not establish container packaging |
| Kubernetes release lifecycle | Docker, kind, kubectl and Helm | `--kubernetes-mysql-image` runs the real local cluster lifecycle; it does not establish production infrastructure or cross-release schema compatibility |
| Deployed CreateDBInstance latency | Explicit MCP URL/token, MySQL/PostgreSQL metadata and identified performance environment | The local load helper runs the original case on both databases; local measurements do not establish deployed latency |

Configure each opt-in test only against an owned disposable environment. A
mock or static contract pass must not be reported as live service, container,
cluster or production performance acceptance. For browser coverage, see the
[Playwright guide](../../docs/en/operations/browser-e2e.md).
