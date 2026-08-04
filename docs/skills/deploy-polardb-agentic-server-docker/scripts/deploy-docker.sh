#!/usr/bin/env bash
# One-click Docker Compose deployment of PolarDB Tool Agentic Server (PAS) on an
# Aliyun ECS. Prerequisite: the ECS can reach PolarDB MySQL (whitelist
# configured) and has outbound internet access.
# Usage: POLARDB_HOST=... POLARDB_USER=... POLARDB_PASSWORD=... bash deploy-docker.sh
set -euo pipefail

: "${POLARDB_HOST:?missing POLARDB_HOST, e.g. pc-xxxx.mysql.polardb.rds.aliyuncs.com}"
: "${POLARDB_USER:?missing POLARDB_USER}"
: "${POLARDB_PASSWORD:?missing POLARDB_PASSWORD}"

POLARDB_PORT="${POLARDB_PORT:-3306}"
PAS_DB_NAME="${PAS_DB_NAME:-pas_meta}"
PAS_HOME="${PAS_HOME:-/data/polar-mcp}"
PAS_REPO="${PAS_REPO:-https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git}"
PAS_IMAGE="${PAS_IMAGE:-ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:0.0.5}"
PAS_PORT="${PAS_PORT:-18760}"
COMPOSE_FILE="deploy/compose/compose.external-mysql.yaml"
# Direct access to ghcr.io / Docker Hub often times out in China; pull and build through mirrors by default
PYPI_INDEX="${PYPI_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-http://mirrors.aliyun.com/debian}"
DEBIAN_SECURITY_MIRROR="${DEBIAN_SECURITY_MIRROR:-http://mirrors.aliyun.com/debian-security}"
REGISTRY_MIRRORS='["https://docker.m.daocloud.io","https://docker.nju.edu.cn","https://docker.mirrors.ustc.edu.cn","https://mirror.baidubce.com"]'

SECRETS_DIR="$PAS_HOME/.secrets"
# The Dockerfile's RUN --mount requires BuildKit; some distros do not enable it by default
export DOCKER_BUILDKIT=1
export POLARDB_HOST POLARDB_PORT POLARDB_USER POLARDB_PASSWORD PAS_DB_NAME

log()   { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
fatal() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null && SUDO="sudo" || fatal "need root or sudo"
fi

pkg_install() {
  if   command -v dnf >/dev/null;      then $SUDO dnf install -y "$@"
  elif command -v yum >/dev/null;      then $SUDO yum install -y "$@"
  elif command -v apt-get >/dev/null;  then $SUDO apt-get update -y && $SUDO apt-get install -y "$@"
  else fatal "no supported package manager (dnf/yum/apt-get)"; fi
}

# ---- 1. PolarDB connectivity preflight ----
log "checking PolarDB $POLARDB_HOST:$POLARDB_PORT ..."
python3 - <<'PY' || fatal "cannot reach PolarDB. Check whitelist (ECS IP), endpoint, and VPC"
import os, socket
socket.create_connection((os.environ["POLARDB_HOST"], int(os.environ["POLARDB_PORT"])), timeout=5).close()
PY

# ---- 2. Docker and the compose plugin ----
command -v curl >/dev/null || pkg_install curl
command -v git  >/dev/null || pkg_install git
if ! command -v docker >/dev/null; then
  log "installing docker ..."
  if   command -v dnf >/dev/null || command -v yum >/dev/null; then pkg_install docker docker-compose-plugin
  else pkg_install docker.io docker-compose-v2 || pkg_install docker.io docker-compose-plugin; fi
fi
docker compose version >/dev/null 2>&1 || pkg_install docker-compose-plugin
# The Dockerfile's RUN --mount needs BuildKit; some distro repos lack a buildx
# package, so fall back to the Aliyun docker-ce mirror (GitHub releases are
# extremely slow in China and are not contacted directly)
if ! docker buildx version >/dev/null 2>&1; then
  if ! pkg_install docker-buildx-plugin 2>/dev/null; then
    if command -v dnf >/dev/null || command -v yum >/dev/null; then
      cat > /etc/yum.repos.d/docker-ce-aliyun.repo <<'REPO'
[docker-ce-aliyun]
name=Docker CE Aliyun Mirror
baseurl=https://mirrors.aliyun.com/docker-ce/linux/centos/9/x86_64/stable/
enabled=1
gpgcheck=0
REPO
      pkg_install docker-buildx-plugin
    else
      pkg_install docker-buildx
    fi
  fi
  # Some distro clients do not scan /usr/libexec; add a symlink
  if ! docker buildx version >/dev/null 2>&1 && [ -x /usr/libexec/docker/cli-plugins/docker-buildx ]; then
    mkdir -p /usr/local/lib/docker/cli-plugins
    ln -sf /usr/libexec/docker/cli-plugins/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx
  fi
  docker buildx version >/dev/null 2>&1 || fatal "docker buildx unavailable"
fi
$SUDO systemctl enable --now docker 2>/dev/null || $SUDO service docker start 2>/dev/null || true
docker info >/dev/null 2>&1 || fatal "docker daemon not running"

# ---- 3. Configure registry mirrors (left untouched if already configured) ----
if [ ! -f /etc/docker/daemon.json ] || ! grep -q registry-mirrors /etc/docker/daemon.json; then
  log "configuring registry mirrors in /etc/docker/daemon.json ..."
  REGISTRY_MIRRORS="$REGISTRY_MIRRORS" python3 - <<'PY'
import json, os
path = "/etc/docker/daemon.json"
try:
    conf = json.load(open(path))
except Exception:
    conf = {}
conf["registry-mirrors"] = json.loads(os.environ["REGISTRY_MIRRORS"])
with open(path, "w") as f:
    json.dump(conf, f, indent=2)
PY
  $SUDO systemctl restart docker
  sleep 3
fi

# ---- 4. Fetch the code (needed for both local image builds and the compose file) ----
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
[ -f "$COMPOSE_FILE" ] || fatal "$COMPOSE_FILE not found in repo"

# ---- 5. Obtain the PAS image: pull first, build locally on timeout ----
EFFECTIVE_IMAGE="$PAS_IMAGE"
if docker image inspect "$PAS_IMAGE" >/dev/null 2>&1; then
  log "image $PAS_IMAGE already present locally"
elif timeout 150 docker pull "$PAS_IMAGE"; then
  log "pulled $PAS_IMAGE"
else
  LOCAL_TAG="pas-local:${PAS_IMAGE##*:}"
  log "pull failed (cross-border ghcr timeouts are common), building $LOCAL_TAG from source ..."
  docker build \
    --build-arg DEBIAN_MIRROR="$DEBIAN_MIRROR" \
    --build-arg DEBIAN_SECURITY_MIRROR="$DEBIAN_SECURITY_MIRROR" \
    --build-arg PYPI_INDEX_URL="$PYPI_INDEX" \
    -t "$LOCAL_TAG" .
  EFFECTIVE_IMAGE="$LOCAL_TAG"
fi
log "using image: $EFFECTIVE_IMAGE"

# ---- 6. Root encryption key: losing it makes encrypted data unrecoverable; reuse if present ----
mkdir -p "$SECRETS_DIR"
if [ ! -f "$SECRETS_DIR/pas_encryption_key" ]; then
  log "generating root encryption key ..."
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' \
    > "$SECRETS_DIR/pas_encryption_key"
  chmod 600 "$SECRETS_DIR/pas_encryption_key"
fi
ENCRYPTION_KEY=$(tr -d '\n' < "$SECRETS_DIR/pas_encryption_key")

# ---- 7. Create the metadata database from a throwaway container (image ships asyncmy; no host Python deps needed) ----
log "ensuring metadata database $PAS_DB_NAME exists ..."
docker run --rm --entrypoint python \
  -e POLARDB_HOST -e POLARDB_PORT -e POLARDB_USER -e POLARDB_PASSWORD -e PAS_DB_NAME \
  "$EFFECTIVE_IMAGE" - <<'PY'
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

# ---- 8. Write the compose env file (account/password URL-encoded; key as raw base64) ----
ENCODED_USER=$(python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["POLARDB_USER"], safe=""))')
ENCODED_PASSWORD=$(python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["POLARDB_PASSWORD"], safe=""))')
cat > "$SECRETS_DIR/pas-compose.env" <<EOF
PAS_DATABASE_URL=mysql+asyncmy://$ENCODED_USER:$ENCODED_PASSWORD@$POLARDB_HOST:$POLARDB_PORT/$PAS_DB_NAME
PAS_ENCRYPTION_KEY=$ENCRYPTION_KEY
PAS_PORT=$PAS_PORT
PAS_IMAGE=$EFFECTIVE_IMAGE
EOF
chmod 600 "$SECRETS_DIR/pas-compose.env"

# ---- 9. Start (server starts only after migrate exits successfully) ----
# Shell environment variables take precedence over --env-file during compose
# interpolation; clear any same-name variables that may have leaked in
unset PAS_DATABASE_URL PAS_ENCRYPTION_KEY PAS_PORT 2>/dev/null || true
COMPOSE="docker compose --env-file $SECRETS_DIR/pas-compose.env -f $COMPOSE_FILE"
log "starting compose stack ..."
$COMPOSE config --quiet
$COMPOSE up -d

# ---- 10. Wait for readiness ----
log "waiting for /readyz ..."
for i in $(seq 1 90); do
  curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$PAS_PORT/readyz" && break
  [ "$i" -eq 90 ] && { $COMPOSE logs --tail=50 migrate server; fatal "server not ready within 180s"; }
  sleep 2
done

# ---- 11. Issue a bootstrap token (valid 15 minutes; issuing invalidates the old one) ----
log "issuing bootstrap token ..."
BOOTSTRAP_TOKEN=$($COMPOSE exec -T server sh -c \
  'pas config bootstrap-token issue --output /tmp/.bt && cat /tmp/.bt' 2>/dev/null \
  | tr -d '\r' || true)
if [ -z "$BOOTSTRAP_TOKEN" ]; then
  BOOTSTRAP_TOKEN=$($COMPOSE logs server 2>/dev/null | grep "Bootstrap token:" | tail -1 | awk '{print $NF}' || true)
fi
[ -n "$BOOTSTRAP_TOKEN" ] && printf '%s\n' "$BOOTSTRAP_TOKEN" > "$SECRETS_DIR/bootstrap_token.txt"
chmod 600 "$SECRETS_DIR/bootstrap_token.txt" 2>/dev/null || true

PRIVATE_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PUBLIC_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || true)
ACCESS_IP="${PUBLIC_IP:-$PRIVATE_IP}"

cat <<EOF

============================================================
 PAS deployed successfully (Docker)

 Web console / API / MCP: http://$ACCESS_IP:$PAS_PORT
   (single port; MCP endpoint: /mcp)
 Private IP: http://$PRIVATE_IP:$PAS_PORT
 Image: $EFFECTIVE_IMAGE

 Bootstrap token (valid 15 min): $BOOTSTRAP_TOKEN
 Backup: $SECRETS_DIR/bootstrap_token.txt

 Next steps:
 1. Open TCP $PAS_PORT in the ECS security group; restrict the
    source to your office network where possible.
 2. Open the console, enter the token, create the first admin.
 3. Register PolarDB instances -> create Agent -> issue Token -> connect MCP client.

 Manage:
   cd $PAS_HOME
   docker compose --env-file $SECRETS_DIR/pas-compose.env -f $COMPOSE_FILE ps|logs|up -d|down
============================================================
EOF
