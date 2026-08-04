---
name: deploy-polardb-agentic-server
description: One-click source deployment of the PolarDB Tool Agentic Server (PAS) MCP gateway on an Aliyun ECS that already has network access to a PolarDB MySQL instance. Installs dependencies, clones the repo, initializes the metadata database, runs migrations, and starts the backend plus web console. Use when the user provides PolarDB MySQL connection info (endpoint / port / user / password) and wants to deploy, install, or bring up the PolarDB MCP / agentic server from source on an ECS.
---

# One-click source deployment of PolarDB Tool Agentic Server (PAS)

Brings up PAS from source on an ECS inside the PolarDB VPC: backend on `18760`, web console on `18761`, metadata stored in PolarDB MySQL.

## Prerequisites (tell the user and stop if unmet)

- The current machine IS the target ECS (mainstream Linux, outbound internet access to install dependencies).
- The user has provided PolarDB MySQL connection info: endpoint, account, password (port defaults to 3306).
- The ECS IP is on the PolarDB whitelist (the script runs a TCP preflight and fails fast otherwise).

## Inputs (collect missing ones from the user first)

| Variable | Required | Default |
|----------|----------|---------|
| `POLARDB_HOST` | yes | none, e.g. `pc-xxxx.mysql.polardb.rds.aliyuncs.com` |
| `POLARDB_USER` | yes | none |
| `POLARDB_PASSWORD` | yes | none (raw password; the script URL-encodes it) |
| `POLARDB_PORT` | no | `3306` |
| `PAS_DB_NAME` | no | `pas_meta` |
| `PAS_HOME` | no | `/data/polar-mcp` |
| `SKIP_WEB` | no | `0` (`1` = skip the web console) |

## Run

```bash
POLARDB_HOST='<endpoint>' POLARDB_USER='<user>' POLARDB_PASSWORD='<password>' \
  bash scripts/deploy.sh
```

The script is idempotent and safe to re-run (code upgrade / service restart / metadata-DB switch). An existing root encryption key and database are reused, never recreated.

What it does, in order: PolarDB TCP preflight -> install git/uv/Python/Node when missing -> clone or update the repo -> `uv sync` (defaults to the Aliyun PyPI mirror; direct PyPI access stalls on Aliyun ECS) -> generate the root encryption key (mode 0600, first run only) -> create the metadata database -> write `.secrets/pas.env` -> `alembic upgrade head` -> start the backend in the background and wait for readiness -> start the web console -> issue a bootstrap token.

## After deployment, tell the user

The script prints a summary; relay it including:

- Web console `http://<ECS public IP>:18761`, backend `http://<ECS public IP>:18760`
- The bootstrap token and its backup at `$PAS_HOME/.secrets/bootstrap_token.txt`
- **The token is valid for 15 minutes** and must be used immediately
- Security group: allow inbound TCP `18761` (required for browsers); allow `18760` only when MCP clients connect directly; restrict sources to office networks where possible
- Follow-up flow: register PolarDB instances -> create an Agent -> issue a Token -> connect an MCP client

## Common operations

```bash
cd /data/polar-mcp && set -a && . .secrets/pas.env && set +a

# Re-issue a token after expiry (invalidates the old one immediately)
uv run pas config bootstrap-token issue --output /data/polar-mcp/.secrets/bootstrap_token_new.txt

# Logs
tail -f log/alibabacloud-polardb-tool-agentic-server.log   # persistent backend log
tail -f run/backend.out run/web.out                        # console output
```

## Troubleshooting

| Symptom | Action |
|---------|--------|
| TCP preflight fails | Check the PolarDB whitelist includes the ECS IP, the endpoint is correct, and they share a VPC |
| `uv sync` stalls | Override with `PYPI_INDEX`; the default is already the Aliyun mirror |
| Node version < 20 | Install Node 20+ manually, or rerun with `SKIP_WEB=1` for backend only |
| Backend never ready | Check `run/backend.out`; usually the connection string in `pas.env` is unreachable or the key file permissions are wrong |
