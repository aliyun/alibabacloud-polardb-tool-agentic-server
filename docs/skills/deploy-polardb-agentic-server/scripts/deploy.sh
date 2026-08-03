#!/usr/bin/env bash
# One-click deployment of PolarDB Tool Agentic Server (PAS) on an Aliyun ECS.
# Prerequisite: the ECS can reach PolarDB MySQL (whitelist configured) and has
# outbound internet access to install dependencies.
# Usage: POLARDB_HOST=... POLARDB_USER=... POLARDB_PASSWORD=... bash deploy.sh
set -euo pipefail

: "${POLARDB_HOST:?missing POLARDB_HOST, e.g. pc-xxxx.mysql.polardb.rds.aliyuncs.com}"
: "${POLARDB_USER:?missing POLARDB_USER}"
: "${POLARDB_PASSWORD:?missing POLARDB_PASSWORD}"

POLARDB_PORT="${POLARDB_PORT:-3306}"
PAS_DB_NAME="${PAS_DB_NAME:-pas_meta}"
PAS_HOME="${PAS_HOME:-/data/polar-mcp}"
PAS_REPO="${PAS_REPO:-https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git}"
SKIP_WEB="${SKIP_WEB:-0}"
# Direct access to PyPI/npm official registries often stalls on Aliyun ECS; use mirrors by default
PYPI_INDEX="${PYPI_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

BACKEND_PORT=18760
WEB_PORT=18761
SECRETS_DIR="$PAS_HOME/.secrets"
RUN_DIR="$PAS_HOME/run"

export PATH="$HOME/.local/bin:$PATH"
export POLARDB_HOST POLARDB_PORT POLARDB_USER POLARDB_PASSWORD PAS_DB_NAME

log()   { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
fatal() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null && SUDO="sudo" || fatal "need root or sudo to install system packages"
fi

pkg_install() {
  if   command -v dnf >/dev/null;      then $SUDO dnf install -y "$@"
  elif command -v yum >/dev/null;      then $SUDO yum install -y "$@"
  elif command -v apt-get >/dev/null;  then $SUDO apt-get update -y && $SUDO apt-get install -y "$@"
  else fatal "no supported package manager (dnf/yum/apt-get)"; fi
}

wait_port() { # <url> <service-name> <pid-file> <log-file>
  for i in $(seq 1 60); do
    curl -fsS -o /dev/null --max-time 2 "$1" && return 0
    if ! pgrep -F "$3" >/dev/null 2>&1; then
      tail -50 "$4" || true
      fatal "$2 exited during startup"
    fi
    sleep 1
  done
  tail -50 "$4" || true
  fatal "$2 not ready within 60s"
}

# ---- 1. PolarDB connectivity preflight ----
log "checking PolarDB $POLARDB_HOST:$POLARDB_PORT ..."
python3 - <<'PY' || fatal "cannot reach PolarDB. Check whitelist (ECS IP), endpoint, and VPC"
import os, socket
socket.create_connection((os.environ["POLARDB_HOST"], int(os.environ["POLARDB_PORT"])), timeout=5).close()
PY

# ---- 2. System dependencies ----
command -v curl >/dev/null    || pkg_install curl
command -v python3 >/dev/null || pkg_install python3
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fatal "Python 3.11+ required, found: $(python3 --version 2>&1)"
command -v git >/dev/null || { log "installing git ..."; pkg_install git; }
if ! command -v uv >/dev/null; then
  log "installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ "$SKIP_WEB" != "1" ]; then
  if ! command -v npm >/dev/null; then
    log "installing Node.js ..."
    pkg_install nodejs npm
  fi
  node_major="$(node -e 'process.stdout.write(String(parseInt(process.versions.node)))' 2>/dev/null || echo 0)"
  [ "$node_major" -ge 20 ] || fatal "Node.js 20+ required (found $(node --version 2>/dev/null || echo none)). Install it or rerun with SKIP_WEB=1"
fi

# ---- 3. Fetch the code ----
if [ -d "$PAS_HOME/.git" ]; then
  log "updating repo in $PAS_HOME ..."
  git -C "$PAS_HOME" pull --ff-only || log "WARN: git pull failed, continuing with existing code"
elif [ -e "$PAS_HOME" ] && [ -n "$(ls -A "$PAS_HOME" 2>/dev/null)" ]; then
  fatal "$PAS_HOME exists but is not a git clone of PAS; set PAS_HOME to another directory"
else
  log "cloning $PAS_REPO -> $PAS_HOME ..."
  git clone "$PAS_REPO" "$PAS_HOME"
fi
cd "$PAS_HOME"

# ---- 4. Python dependencies ----
log "uv sync (index: $PYPI_INDEX) ..."
UV_INDEX_URL="$PYPI_INDEX" uv sync

# ---- 5. Root encryption key: losing it makes encrypted credentials unrecoverable; reuse if present ----
mkdir -p "$SECRETS_DIR" "$RUN_DIR" "$PAS_HOME/data"
if [ ! -f "$SECRETS_DIR/pas_encryption_key" ]; then
  log "generating root encryption key ..."
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' \
    > "$SECRETS_DIR/pas_encryption_key"
  chmod 600 "$SECRETS_DIR/pas_encryption_key"
fi

# ---- 6. Create the metadata database ----
log "ensuring metadata database $PAS_DB_NAME exists ..."
"$PAS_HOME/.venv/bin/python" - <<'PY'
import asyncio, os, asyncmy

async def main():
    conn = await asyncmy.connect(
        host=os.environ["POLARDB_HOST"], port=int(os.environ["POLARDB_PORT"]),
        user=os.environ["POLARDB_USER"], password=os.environ["POLARDB_PASSWORD"])
    async with conn.cursor() as cur:
        await cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{os.environ['PAS_DB_NAME']}` DEFAULT CHARACTER SET utf8mb4")
    conn.close()

asyncio.run(main())
PY

# ---- 7. Write bootstrap settings (account and password are URL-encoded) ----
ENCODED_USER=$(python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["POLARDB_USER"], safe=""))')
ENCODED_PASSWORD=$(python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["POLARDB_PASSWORD"], safe=""))')
cat > "$SECRETS_DIR/pas.env" <<EOF
PAS_DATABASE_URL=mysql+asyncmy://$ENCODED_USER:$ENCODED_PASSWORD@$POLARDB_HOST:$POLARDB_PORT/$PAS_DB_NAME
PAS_ENCRYPTION_KEY=file:$SECRETS_DIR/pas_encryption_key
EOF
chmod 600 "$SECRETS_DIR/pas.env"
set -a; . "$SECRETS_DIR/pas.env"; set +a

# ---- 8. Database migration ----
log "running alembic migrations ..."
uv run alembic upgrade head

# ---- 9. Start/restart the backend ----
log "starting backend on :$BACKEND_PORT ..."
[ -f "$RUN_DIR/backend.pid" ] && kill "$(cat "$RUN_DIR/backend.pid")" 2>/dev/null || true
pkill -f "python -m server" 2>/dev/null || true
sleep 2
nohup uv run python -m server > "$RUN_DIR/backend.out" 2>&1 &
echo $! > "$RUN_DIR/backend.pid"
wait_port "http://127.0.0.1:$BACKEND_PORT/health" backend "$RUN_DIR/backend.pid" "$RUN_DIR/backend.out"

# ---- 10. Web console ----
if [ "$SKIP_WEB" != "1" ]; then
  log "starting web console on :$WEB_PORT ..."
  cd "$PAS_HOME/web"
  [ -d node_modules ] || npm install --registry="$NPM_REGISTRY"
  [ -f "$RUN_DIR/web.pid" ] && kill "$(cat "$RUN_DIR/web.pid")" 2>/dev/null || true
  pkill -f "vite dev" 2>/dev/null || true
  sleep 1
  nohup npm run dev -- --host 0.0.0.0 > "$RUN_DIR/web.out" 2>&1 &
  echo $! > "$RUN_DIR/web.pid"
  wait_port "http://127.0.0.1:$WEB_PORT/" web-console "$RUN_DIR/web.pid" "$RUN_DIR/web.out"
  cd "$PAS_HOME"
fi

# ---- 11. Issue a bootstrap token (valid 15 minutes; issuing invalidates the old one) ----
log "issuing bootstrap token ..."
rm -f "$SECRETS_DIR/bootstrap_token.txt"
if ! uv run pas config bootstrap-token issue --output "$SECRETS_DIR/bootstrap_token.txt" >/dev/null 2>&1; then
  grep "Bootstrap token:" "$RUN_DIR/backend.out" | tail -1 | awk '{print $NF}' \
    > "$SECRETS_DIR/bootstrap_token.txt" || true
fi
BOOTSTRAP_TOKEN=$(cat "$SECRETS_DIR/bootstrap_token.txt" 2>/dev/null || true)

PRIVATE_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PUBLIC_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || true)
ACCESS_IP="${PUBLIC_IP:-$PRIVATE_IP}"

cat <<EOF

============================================================
 PAS deployed successfully

 Web console : http://$ACCESS_IP:$WEB_PORT
 Backend/MCP : http://$ACCESS_IP:$BACKEND_PORT  (MCP endpoint: /mcp)
 Private IP  : http://$PRIVATE_IP:$WEB_PORT

 Bootstrap token (valid 15 min): $BOOTSTRAP_TOKEN
 Backup: $SECRETS_DIR/bootstrap_token.txt

 Next steps:
 1. Open TCP $WEB_PORT in the ECS security group (browser access).
    Open TCP $BACKEND_PORT only when MCP clients connect directly.
    Restrict sources to your office network where possible.
 2. Open the web console, enter the token, create the first admin.
 3. Register PolarDB instances -> create Agent -> issue Token -> connect MCP client.

 Re-issue token after expiry:
   cd $PAS_HOME && set -a && . .secrets/pas.env && set +a && \\
   uv run pas config bootstrap-token issue --output $SECRETS_DIR/bootstrap_token_new.txt
============================================================
EOF
