---
name: deploy-polardb-agentic-server-docker
description: One-click Docker Compose deployment of the PolarDB Tool Agentic Server (PAS) on an Aliyun ECS that already has network access to a PolarDB MySQL instance. Installs Docker, configures registry mirrors for China networks, pulls or locally builds the PAS image, wires the external PolarDB metadata database, and starts the stack. Use when the user provides PolarDB MySQL connection info and wants a Docker / container / compose deployment of the PolarDB MCP / agentic server, or a production-like single-host setup.
---

# One-click Docker deployment of PolarDB Tool Agentic Server (PAS)

Brings up PAS with Docker Compose on an ECS inside the PolarDB VPC. **Key difference from the source-deployment skill**: the image ships the built web console, so console, API, and MCP all share the single port `18760`; no host Node/Python environment is needed, and containers restart automatically with Docker (`restart: unless-stopped`).

## Prerequisites (tell the user and stop if unmet)

- The current machine IS the target ECS (mainstream Linux, outbound internet access).
- The user has provided PolarDB MySQL connection info: endpoint, account, password (port defaults to 3306).
- The ECS IP is on the PolarDB whitelist (the script runs a TCP preflight and fails fast otherwise).

## Inputs (collect missing ones from the user first)

| Variable | Required | Default |
|----------|----------|---------|
| `POLARDB_HOST` | yes | none |
| `POLARDB_USER` | yes | none |
| `POLARDB_PASSWORD` | yes | none (raw password; the script URL-encodes it) |
| `POLARDB_PORT` | no | `3306` |
| `PAS_DB_NAME` | no | `pas_meta` |
| `PAS_HOME` | no | `/data/polar-mcp` |
| `PAS_IMAGE` | no | `ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:0.0.5` |
| `PAS_PORT` | no | `18760` |

## Run

```bash
POLARDB_HOST='<endpoint>' POLARDB_USER='<user>' POLARDB_PASSWORD='<password>' \
  bash scripts/deploy-docker.sh
```

The script is idempotent and safe to re-run (image upgrade / service restart / metadata-DB switch). An existing root encryption key is reused, never recreated.

What it does, in order: PolarDB TCP preflight -> install Docker + compose plugin when missing -> install buildx when missing (via the Aliyun docker-ce mirror; GitHub releases are too slow in China) -> configure registry mirrors in daemon.json (skipped if already configured) -> clone or update the repo -> obtain the PAS image (pull from ghcr first, fall back to a local `docker build` with Aliyun debian/PyPI mirrors) -> reuse or generate the root encryption key -> create the metadata database from a throwaway container -> write `.secrets/pas-compose.env` (mode 0600) -> start the official `deploy/compose/compose.external-mysql.yaml` stack (server starts only after migrate succeeds) -> wait for `/readyz` -> issue a bootstrap token inside the container.

## After deployment, tell the user

- Access URL: `http://<ECS public IP>:18760` (console / API / MCP endpoint `/mcp` on the same port)
- The bootstrap token and its backup at `$PAS_HOME/.secrets/bootstrap_token.txt`; **valid for 15 minutes**
- Security group: only TCP `18760` is needed; restrict sources to office networks where possible
- Follow-up flow: register PolarDB instances -> create an Agent -> issue a Token -> connect an MCP client

## Common operations

```bash
cd /data/polar-mcp
C="docker compose --env-file .secrets/pas-compose.env -f deploy/compose/compose.external-mysql.yaml"

$C ps                     # status
$C logs -f server         # logs
$C up -d                  # restart / apply new env
docker compose down       # stop (volumes are kept)

# Re-issue a token inside the container after expiry (invalidates the old one)
$C exec -T server sh -c 'pas config bootstrap-token issue --output /tmp/bt && cat /tmp/bt'

# Upgrade: after changing PAS_IMAGE
$C pull && $C run --rm migrate database migrate && $C up -d --no-deps server
```

## Troubleshooting

| Symptom | Action |
|---------|--------|
| TCP preflight fails | Check the PolarDB whitelist, endpoint, and shared VPC |
| ghcr.io pull stalls | Expected across borders; the script falls back to a local build. You can also set `PAS_IMAGE` to a pre-mirrored registry |
| Base images will not pull | Confirm registry-mirrors in `/etc/docker/daemon.json` are effective (visible in `docker info`) |
| `migrate` fails | `docker compose logs migrate`; usually an unreachable connection string or an encryption key that does not match the data in the database |
| server refuses to start | Migrations have not reached the required alembic head; run `$C run --rm migrate database migrate` manually |
