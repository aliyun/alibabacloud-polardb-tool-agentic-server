# Agent-assisted single-host deployment

**English** | [简体中文](../../zh-cn/deployment/agent-assisted-deployment.md)

The repository includes one deployment skill for explicitly requested,
single-host PAS deployment. The skill selects either Docker Compose or source
mode, validates the Linux target before mutation, and defaults to PAS `0.0.5`.

## Scope and agent discovery

The canonical skill is
`.agents/skills/deploy-polardb-agentic-server/SKILL.md`. Codex and Cursor can
discover that Agent Skills path. Claude Code uses the synchronized copy under
`.claude/skills/`; its metadata also disables model-initiated invocation.

Invoke `deploy-polardb-agentic-server` explicitly and choose one mode:

- Docker Compose for a production-like single-host deployment with the console
  and API on one port.
- Source for development, code customization, backend-only operation, or a
  target without Docker.

The scripts target Linux. A Mac can act as the control workstation over SSH,
but is not a native PAS deployment target.

## Safety and release pin

The default release settings are:

```bash
PAS_VERSION=0.0.5
PAS_REF=v${PAS_VERSION}
PAS_IMAGE=ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:${PAS_VERSION}
```

New and updated checkouts fetch `PAS_REF` and use detached HEAD. When
`PAS_UPDATE_REPO=1`, an existing checkout must be clean and its `origin` must
match `PAS_REPO`. The scripts do not follow the repository's default branch.

Do not put a database password, connection URL, encryption key, or bootstrap
token in an agent conversation or command line. On the target, write the
database password to an owner-readable file (`0600`) and pass its path through
`POLARDB_PASSWORD_FILE`. The scripts store the bootstrap token in
`$PAS_HOME/.secrets/bootstrap_token.txt`; read it only on the target.

## Validate the Linux target

From a checkout containing the skill, a Mac or Linux control host can stream a
validation script over SSH standard input. This does not use `scp` and does not
copy a secret:

```bash
SKILL_DIR=.agents/skills/deploy-polardb-agentic-server
ssh user@linux-host \
  "POLARDB_HOST='db-endpoint' POLARDB_USER='pas_user' \
   PAS_HOME='/data/polar-mcp' bash -s -- --validate-only" \
  < "$SKILL_DIR/scripts/deploy-docker.sh"
```

Use `deploy-source.sh` instead only after selecting source mode. Validation
checks Linux, inputs, database TCP reachability, target-directory safety,
repository identity when present, and the mode-specific runtime path. It does
not read a password or change packages, files, images, processes, or services.

## Run the selected mode

After validation passes and the operator approves mutation, create the password
file directly on the Linux target, then run the same script without
`--validate-only`. For example, Docker mode can be streamed without copying the
script:

```bash
ssh user@linux-host \
  "POLARDB_HOST='db-endpoint' POLARDB_USER='pas_user' \
   POLARDB_PASSWORD_FILE='/secure/polardb-password' \
   PAS_HOME='/data/polar-mcp' bash -s" \
  < "$SKILL_DIR/scripts/deploy-docker.sh"
```

Docker mode defaults to the published `0.0.5` image and fails if the image
cannot be pulled. Set `PAS_IMAGE` to an approved fully qualified mirror when
needed. Source mode builds the pinned `v0.0.5` checkout with its frozen Python
lock; use `SKIP_WEB=1` for backend-only deployment.

## Explicit expert overrides

`PAS_UPDATE_REPO=0` preserves a deliberately pre-positioned PAS checkout and
skips fetch and checkout. Project markers are still verified, but the operator
then owns commit provenance and dependency compatibility.

Docker mode builds locally only when `PAS_ALLOW_LOCAL_BUILD=1` is explicit. A
local build uses the same verified checkout, but it is not equivalent to using
the published image provenance. Do not enable the fallback merely to bypass a
registry or architecture error.

## Verification, rollback, and removal

Verify `http://127.0.0.1:18760/readyz` after either mode. Docker operators
should also inspect the Compose project; source operators should inspect
`run/backend.out` and, when enabled, `run/web.out`. Restrict inbound access to
the required sources.

The deployment scripts do not delete data and do not roll back schema
migrations. Before changing versions, back up the metadata database, root key,
and deployment configuration and follow the
[upgrade and rollback guide](upgrade-and-rollback.md).

To stop Docker mode without deleting `$PAS_HOME`, run:

```bash
cd /data/polar-mcp
docker compose -p polardb-agentic \
  --env-file .secrets/pas-compose.env \
  -f deploy/compose/compose.external-mysql.yaml down
```

For source mode, verify each recorded PID's `/proc/<pid>/cwd` and command line
before stopping it; never use broad `pkill` patterns. Remove `$PAS_HOME` only
after the operator has separately preserved or intentionally discarded its
secrets, logs, and data. Database cleanup is a separate, explicit operation.
