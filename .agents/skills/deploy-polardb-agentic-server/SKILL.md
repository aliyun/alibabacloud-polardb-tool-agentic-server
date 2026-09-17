---
name: deploy-polardb-agentic-server
description: Use when the user explicitly asks to deploy PolarDB Tool Agentic Server (PAS) on a Linux host, including Docker Compose, container, production-like, source, or development deployments, or to complete PolarRAG MCP delivery after PAS deployment.
license: Apache-2.0
metadata:
  author: aliyun
  version: "1.5"
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
| `PAS_DATABASE_ENGINE` | no | `mysql` |
| `POLARDB_HOST` | MySQL only | none |
| `POLARDB_USER` | MySQL only | none |
| `POLARDB_PORT` | MySQL only | `3306` |
| `PAS_DB_NAME` | MySQL only | `pas_meta` |
| `PAS_SQLITE_VOLUME` | SQLite only | `${PAS_COMPOSE_PROJECT}-sqlite-data` |
| `PAS_PORT` | no | `18760` |
| `PAS_HOME` | no | `/data/polar-mcp` |
| `PAS_VERSION` | no | bundled release |
| `PAS_REF` | no | `v${PAS_VERSION}` |
| `PAS_REPO` | no | official GitHub repository |
| `PAS_UPDATE_REPO` | no | `1` |

`PAS_DATABASE_ENGINE` is Docker-only. Its default `mysql` preserves the existing
external-MySQL contract, including `POLARDB_PASSWORD_FILE=/path/to/mode-0600-file`
or an interactive password prompt. `POLARDB_PASSWORD` is supported for
non-interactive MySQL automation but must not be placed in a command line or
agent conversation.

Set `PAS_DATABASE_ENGINE=sqlite` explicitly only for a clean single-host
deployment that does not use MySQL. SQLite mode rejects every `POLARDB_*`
input, needs no database password or host SQLite installation, and stores its
database and root key in the persistent Docker named volume
`PAS_SQLITE_VOLUME`. The volume is external to Compose, so both `docker compose
down` and `docker compose down -v` retain it; deletion requires a separate,
explicit Docker volume action. Initialization holds an exclusive volume lock
and never replaces an existing root key. Do not use the SQLite mode for Source
deployment.

The default path fetches and checks out the immutable release selected by `PAS_REF` in detached-HEAD mode. An existing checkout must be clean and its `origin` must match `PAS_REPO`. Set `PAS_UPDATE_REPO=0` only for a deliberately pre-positioned PAS checkout; this expert override keeps the current commit but still verifies PAS project markers.

## Required workflow

Resolve this skill's directory first; script paths below are relative to that directory.

1. Select one mode from observable request details and state the selection.
2. Run that mode's validation before making changes:

   Docker (`PAS_IMAGE` and `PAS_PORT`, default `18760`, are optional):

   ```bash
   POLARDB_HOST='<endpoint>' POLARDB_USER='<account>' \
     bash scripts/deploy-docker.sh --validate-only
   ```

   Explicit Docker SQLite with an optional alternate host port:

   ```bash
   PAS_DATABASE_ENGINE=sqlite PAS_PORT=18782 \
     bash scripts/deploy-docker.sh --validate-only
   ```

   Source (`SKIP_WEB=1` is optional):

   ```bash
   POLARDB_HOST='<endpoint>' POLARDB_USER='<account>' \
     bash scripts/deploy-source.sh --validate-only
   ```

3. Report non-secret validation results. Continue only if the target and chosen mode are correct and validation passed.
4. In MySQL mode, on the target host, set `POLARDB_PASSWORD_FILE` or let the
   selected script prompt on its TTY. SQLite mode has no database password.
   Remove `--validate-only` from the validated command; do not switch scripts.
5. Verify the selected mode:

   - Docker: verify `http://127.0.0.1:${PAS_PORT:-18760}/readyz` and inspect Compose service status.
   - Source: verify `http://127.0.0.1:18760/readyz`; unless `SKIP_WEB=1`, also verify `http://127.0.0.1:18761/`.

6. Report URLs, mode-specific status, and `$PAS_HOME/.secrets/bootstrap_token.txt` only. The operator must read the token directly in the target terminal; it expires after 15 minutes.

## PolarRAG MCP version compatibility

**Legacy v0.0.7 upload contract.** Release `v0.0.7` is pinned at
`9d358cd813cf07979a23cae7d80b3442231a1adb`.
`prepare_document_upload`, `resume_document_upload`,
`complete_document_upload`, and `abort_document_upload` are exposed, and
`complete_document_upload` requires both `upload_session_id` and `parts`.
Use that checkout's `docs/en/knowledge/polarrag-mcp.md`; do not apply the
current onboarding upload steps to that legacy release.

**Current onboarding upload contract.** The bundled release exposes only
`prepare_document_upload` and `complete_document_upload`. Only `upload_session_id` is accepted by `complete_document_upload`; PAS validates multipart parts itself. Run the current onboarding only on an immutable
`v0.0.8` or later `PAS_REF` whose checkout contains
`$PAS_HOME/docs/en/knowledge/polarrag-onboarding.md` and whose user Agent
MCP catalog has this two-Tool schema. Never substitute the moving `develop`
branch for that immutable ref.

For a source deployment, explicitly set that immutable `PAS_REF`. For Docker,
also set an approved `PAS_IMAGE` built from the same ref, or explicitly set
`PAS_ALLOW_LOCAL_BUILD=1` to force a local build of that checked-out ref; this
does not inspect or pull the default image. Local builds require Docker Buildx:
the script checks it before it creates or updates `PAS_HOME`. If unavailable,
install a trusted Docker Buildx plugin, or use an approved `PAS_IMAGE` with
`PAS_ALLOW_LOCAL_BUILD=0`; the script never downloads or installs Buildx. When
Docker is already available, `--validate-only` performs this check without host
changes. On the target, stop
before onboarding if the required document file is absent or the MCP catalog
does not match the current onboarding upload contract.

## Optional PolarRAG MCP delivery

Run this phase only when the user explicitly requests PolarRAG onboarding or end-to-end PolarRAG MCP delivery and the current onboarding upload contract passes the compatibility checks above. A healthy PAS deployment does not prove that PolarRAG registration, authorization, upload, or MCP access works.

Follow `$PAS_HOME/docs/en/knowledge/polarrag-onboarding.md` (or its Simplified Chinese counterpart) and preserve these boundaries:

- Do not deploy or reconfigure PolarRAG, OpenSearch Security, cloud firewalls, PolarDB whitelists, or OSS policies. Report the exact upstream prerequisite for the operator to handle.
- Do not ask for or relay the OpenSearch account password, OSS AccessKey secret, user password, bootstrap token, or Agent Token. The operator enters secrets in the PAS console; the user obtains their own token through the protected reveal or one-time SSO delivery flow.
- Use the native `polarrag` provider for the first delivery. Its `user` principal ID must remain the PAS user's immutable external ID.
- Treat `PUBLIC` and `PERSONAL` as different authorization paths. An Agent's public scope can narrow `PUBLIC`; it cannot grant `PERSONAL`. An administrator must claim a `PERSONAL` knowledge base for the mapped owner.
- Do not give upload tools a local file path or OSS credentials. An approved local upload client transfers bytes to the signed URLs returned by `prepare_document_upload`, then the Agent calls `complete_document_upload`.

Complete the following checklist in order:

1. Finish initial setup with the bootstrap token. Require `/readyz` to return HTTP 200 with `mode=READY` and `config_status=CURRENT`.
2. Register the PolarRAG endpoint, enable and synchronize a Space, and stop if PAS reports `capability_missing`.
3. Create a built-in PAS user and add a native `polarrag` `user` principal in the Space's identity domain.
4. Confirm synchronized `PUBLIC` resources, or claim an `UNCLAIMED` `PERSONAL` resource for that user.
5. Configure the Space's trusted OSS bucket and prefix in the console. Require the credential probe to pass; credentials must be encrypted with `PAS_ENCRYPTION_KEY`.
6. Create an Agent, bind the PolarRAG instance, choose its `PUBLIC` scope, and assign the user. A machine token beginning with `pas_agent_` does not receive PolarRAG tools.
7. Have the user sign in and issue a user Agent Token beginning with `pas_user_agent_`; connect the MCP client with the generated configuration.
8. Verify resource discovery and the expected tools. For upload delivery, exercise `prepare_document_upload` and `complete_document_upload`, then confirm the returned document with `doc_status`.

Accept PolarRAG MCP delivery only when all of these are true:

| Check | Required evidence |
|---|---|
| PAS configuration | `/readyz` reports `READY` and `CURRENT` |
| PolarRAG capability | Instance and Space are active, synchronized, and show no `capability_missing` |
| Identity and ACL | The mapped user sees the intended `PUBLIC` resources and only their claimed `PERSONAL` resources |
| MCP catalog | The `pas_user_agent_` connection exposes the authorized PolarRAG tools, including upload tools when OSS is ready |
| Functional path | The user can list resources and complete the requested search, fetch, management, or upload scenario |

Report PAS deployment and PolarRAG MCP delivery as separate statuses. If this optional phase is incomplete, state the first unmet acceptance check instead of describing the deployment as fully delivered.

## Mode-specific result

- Docker packages the web console, API, and MCP endpoint on `${PAS_PORT:-18760}`.
  MySQL mode uses `deploy/compose/compose.external-mysql.yaml` and the protected
  `$PAS_HOME/.secrets/pas-compose.env`; SQLite mode uses
  `deploy/compose/compose.sqlite.yaml` and has no host-side database/key env
  file. Both modes run the migration before starting the server.
- Docker pulls the image matching the bundled release by default and fails closed if it is unavailable. Use an approved fully qualified `PAS_IMAGE`, or explicitly set `PAS_ALLOW_LOCAL_BUILD=1` to force a local Buildx build of the checked-out `PAS_REF` without pulling the default image.
- Source serves backend/MCP on `18760` and the optional web console on `18761`. Inspect `$PAS_HOME/run/backend.out` and `$PAS_HOME/run/web.out`.
- The bootstrap-token file is mode `0600` and is created only while PAS is in `SETUP` mode.
- Restrict any inbound console or MCP ports to required sources rather than opening them globally.

On failure, inspect the selected mode's reported logs and fix the specific prerequisite. Do not switch modes silently, follow a moving branch, build an image implicitly, scrape tokens from logs, reuse stale code, kill broad process patterns, or weaken registry security.
