---
name: deploy-polardb-agentic-server
description: Use when the user explicitly asks to deploy PolarDB Tool Agentic Server (PAS) on a Linux host, including Docker Compose, container, production-like, source, or development deployments.
license: Apache-2.0
disable-model-invocation: true
metadata:
  author: aliyun
  version: "1.2"
---

# Deploy PAS

Choose exactly one deployment mode, validate it on the intended target, and run only its script.

## Linux target compatibility

The target needs Bash and PolarDB access. Docker mode needs Docker Compose v2. Source mode needs Git, Python 3.11, uv, and optionally Node.js 20. The skill works with Codex, Claude Code, Cursor, and Agent Skills-compatible agents.

## Choose a mode

| Observable request | Mode | Script |
|---|---|---|
| Docker, Compose, container, bundled console, or production-like single host | Docker | `scripts/deploy-docker.sh` |
| Source, development, code customization, backend-only, or Docker is unavailable | Source | `scripts/deploy-source.sh` |

If the request only says to deploy PAS, recommend Docker because it packages the console and API behind one port, then require an explicit choice before mutation. If the request contains conflicting signals, stop and clarify. Never run both modes against the same `PAS_HOME`.

## Safety boundary

- Confirm that commands are running on the intended Linux target. If the current agent is not connected to that host, stop and explain how to run the validation there.
- A macOS workstation may control a Linux target over SSH, but it is not a native deployment target. Stream validation scripts over SSH standard input when needed; do not copy repositories, scripts, or secret files with file-transfer commands unless the user explicitly requests that workflow.
- Do not ask the user to send a database password, token, key, or connection URL through chat. Have the user enter the password in the target terminal, or place it in a target-host file readable only by its owner and set `POLARDB_PASSWORD_FILE`.
- Do not print or relay a database password, connection URL, encryption key, or bootstrap token. Report only the protected file that contains the bootstrap token.
- Do not modify cloud security groups, PolarDB whitelists, or other infrastructure. Report the minimum port and source restrictions for the operator to review.
- In Docker mode, do not change `/etc/docker/daemon.json`, install unverified registry mirrors, or weaken package signature checks. Use `PAS_IMAGE` for an approved fully qualified mirror.

## Common inputs

Supply non-secret inputs as environment variables:

| Variable | Required | Default |
|---|---:|---|
| `POLARDB_HOST` | yes | none |
| `POLARDB_USER` | yes | none |
| `POLARDB_PORT` | no | `3306` |
| `PAS_DB_NAME` | no | `pas_meta` |
| `PAS_HOME` | no | `/data/polar-mcp` |
| `PAS_VERSION` | no | `0.0.5` |
| `PAS_REF` | no | `v${PAS_VERSION}` |
| `PAS_REPO` | no | official GitHub repository |
| `PAS_UPDATE_REPO` | no | `1` |

For the secret, prefer `POLARDB_PASSWORD_FILE=/path/to/mode-0600-file`. An interactive terminal prompt is the fallback. `POLARDB_PASSWORD` is supported for non-interactive automation but must not be placed in a command line or agent conversation.

The default path fetches and checks out the immutable `v0.0.5` release in detached-HEAD mode. An existing checkout must be clean and its `origin` must match `PAS_REPO`. Set `PAS_UPDATE_REPO=0` only for a deliberately pre-positioned PAS checkout; this expert override keeps the current commit but still verifies PAS project markers.

## Required workflow

Resolve this skill's directory first; script paths below are relative to that directory.

1. Select one mode from observable request details and state the selection.
2. Run that mode's validation before making changes:

   Docker (`PAS_IMAGE` and `PAS_PORT`, default `18760`, are optional):

   ```bash
   POLARDB_HOST='<endpoint>' POLARDB_USER='<account>' \
     bash scripts/deploy-docker.sh --validate-only
   ```

   Source (`SKIP_WEB=1` is optional):

   ```bash
   POLARDB_HOST='<endpoint>' POLARDB_USER='<account>' \
     bash scripts/deploy-source.sh --validate-only
   ```

3. Report non-secret validation results. Continue only if the target and chosen mode are correct and validation passed.
4. On the target host, set `POLARDB_PASSWORD_FILE` or let the selected script prompt on its TTY. Remove `--validate-only` from the validated command; do not switch scripts.
5. Verify the selected mode:

   - Docker: verify `http://127.0.0.1:${PAS_PORT:-18760}/readyz` and inspect Compose service status.
   - Source: verify `http://127.0.0.1:18760/readyz`; unless `SKIP_WEB=1`, also verify `http://127.0.0.1:18761/`.

6. Report URLs, mode-specific status, and `$PAS_HOME/.secrets/bootstrap_token.txt` only. The operator must read the token directly in the target terminal; it expires after 15 minutes.

## Mode-specific result

- Docker packages the web console, API, and MCP endpoint on `${PAS_PORT:-18760}`. From `$PAS_HOME`, inspect the generated Compose project with `docker compose --env-file .secrets/pas-compose.env -f deploy/compose/compose.external-mysql.yaml ps`.
- Docker pulls the `0.0.5` image by default and fails closed if it is unavailable. Use an approved fully qualified `PAS_IMAGE`, or explicitly set `PAS_ALLOW_LOCAL_BUILD=1` to build the same pinned checkout locally.
- Source serves backend/MCP on `18760` and the optional web console on `18761`. Inspect `$PAS_HOME/run/backend.out` and `$PAS_HOME/run/web.out`.
- The bootstrap-token file is mode `0600` and is created only while PAS is in `SETUP` mode.
- Restrict any inbound console or MCP ports to required sources rather than opening them globally.

On failure, inspect the selected mode's reported logs and fix the specific prerequisite. Do not switch modes silently, follow a moving branch, build an image implicitly, scrape tokens from logs, reuse stale code, kill broad process patterns, or weaken registry security.
