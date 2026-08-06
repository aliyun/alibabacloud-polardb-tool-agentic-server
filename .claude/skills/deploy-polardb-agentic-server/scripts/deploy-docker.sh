#!/usr/bin/env bash
# Deploy PolarDB Tool Agentic Server with Docker Compose on the target Linux host.
set -euo pipefail
umask 077

VALIDATE_ONLY=0
if [ "${1:-}" = "--validate-only" ]; then
  VALIDATE_ONLY=1
  shift
fi
[ "$#" -eq 0 ] || {
  printf 'Usage: %s [--validate-only]\n' "$0" >&2
  exit 2
}

: "${POLARDB_HOST:?missing POLARDB_HOST}"
: "${POLARDB_USER:?missing POLARDB_USER}"

POLARDB_PORT="${POLARDB_PORT:-3306}"
PAS_DB_NAME="${PAS_DB_NAME:-pas_meta}"
PAS_HOME="${PAS_HOME:-/data/polar-mcp}"
PAS_REPO="${PAS_REPO:-https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server.git}"
PAS_VERSION="${PAS_VERSION:-0.0.6}"
PAS_REF="${PAS_REF:-v${PAS_VERSION}}"
PAS_UPDATE_REPO="${PAS_UPDATE_REPO:-1}"
PAS_IMAGE="${PAS_IMAGE:-ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:${PAS_VERSION}}"
PAS_ALLOW_LOCAL_BUILD="${PAS_ALLOW_LOCAL_BUILD:-0}"
PAS_PORT="${PAS_PORT:-18760}"
PAS_COMPOSE_PROJECT="${PAS_COMPOSE_PROJECT:-polardb-agentic}"
COMPOSE_FILE="deploy/compose/compose.external-mysql.yaml"
PYPI_INDEX="${PYPI_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-http://mirrors.aliyun.com/debian}"
DEBIAN_SECURITY_MIRROR="${DEBIAN_SECURITY_MIRROR:-http://mirrors.aliyun.com/debian-security}"
SECRETS_DIR="$PAS_HOME/.secrets"

log() { printf '[deploy] %s\n' "$*"; }
fatal() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

SUDO=()
DOCKER_COMMAND=(docker)

require_sudo() {
  [ "$(id -u)" -eq 0 ] && return 0
  if [ "${#SUDO[@]}" -eq 0 ]; then
    command -v sudo >/dev/null \
      || fatal "root or sudo is required for this privileged operation"
    SUDO=(sudo)
  fi
}

pkg_install() {
  require_sudo
  if command -v dnf >/dev/null; then
    "${SUDO[@]}" dnf install -y "$@"
  elif command -v yum >/dev/null; then
    "${SUDO[@]}" yum install -y "$@"
  elif command -v apt-get >/dev/null; then
    "${SUDO[@]}" apt-get update -y
    "${SUDO[@]}" apt-get install -y "$@"
  else
    fatal "no supported package manager (dnf, yum, or apt-get)"
  fi
}

validate_inputs() {
  [ "$(uname -s)" = "Linux" ] || fatal "the deployment target must be Linux"
  case "$POLARDB_PORT" in ''|*[!0-9]*) fatal "POLARDB_PORT must be an integer" ;; esac
  [ "$POLARDB_PORT" -ge 1 ] && [ "$POLARDB_PORT" -le 65535 ] \
    || fatal "POLARDB_PORT must be between 1 and 65535"
  case "$PAS_PORT" in ''|*[!0-9]*) fatal "PAS_PORT must be an integer" ;; esac
  [ "$PAS_PORT" -ge 1 ] && [ "$PAS_PORT" -le 65535 ] \
    || fatal "PAS_PORT must be between 1 and 65535"
  case "$PAS_DB_NAME" in
    ''|*[!A-Za-z0-9_]*) fatal "PAS_DB_NAME may contain only letters, numbers, and underscore" ;;
  esac
  [ "${#PAS_DB_NAME}" -le 64 ] || fatal "PAS_DB_NAME must be at most 64 characters"
  case "$POLARDB_HOST" in
    *[[:space:]]*|'') fatal "POLARDB_HOST must be a non-empty hostname or IP without whitespace" ;;
  esac
  case "$POLARDB_USER" in
    *$'\n'*|*$'\r'*|'') fatal "POLARDB_USER must be non-empty and single-line" ;;
  esac
  case "$PAS_HOME" in /*) ;; *) fatal "PAS_HOME must be an absolute path" ;; esac
  [ "$PAS_HOME" != "/" ] || fatal "PAS_HOME cannot be /"
  case "$PAS_COMPOSE_PROJECT" in
    ''|*[!A-Za-z0-9_.-]*) fatal "PAS_COMPOSE_PROJECT contains unsupported characters" ;;
  esac
  [ "$PAS_UPDATE_REPO" = "0" ] || [ "$PAS_UPDATE_REPO" = "1" ] \
    || fatal "PAS_UPDATE_REPO must be 0 or 1"
  [ "$PAS_ALLOW_LOCAL_BUILD" = "0" ] || [ "$PAS_ALLOW_LOCAL_BUILD" = "1" ] \
    || fatal "PAS_ALLOW_LOCAL_BUILD must be 0 or 1"
  case "$PAS_REF" in
    ''|*[[:space:]]*|-*) fatal "PAS_REF must be a non-empty Git ref without whitespace" ;;
  esac
}

normalize_repo_url() {
  local value="${1%/}"
  value="${value%.git}"
  printf '%s\n' "$value"
}

checkout_changes() {
  git -C "$PAS_HOME" status --porcelain --untracked-files=no
  git -C "$PAS_HOME" ls-files --others --exclude-standard | while IFS= read -r path; do
    case "$path" in
      .secrets/*|run/*) ;;
      *) printf '?? %s\n' "$path" ;;
    esac
  done
}

verify_checkout_identity() {
  local origin
  git -C "$PAS_HOME" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fatal "$PAS_HOME is not a valid Git checkout"
  [ -f "$PAS_HOME/pyproject.toml" ] && [ -d "$PAS_HOME/server" ] \
    && [[ "$(<"$PAS_HOME/pyproject.toml")" == *'name = "alibabacloud-polardb-tool-agentic-server"'* ]] \
    || fatal "$PAS_HOME does not contain the expected PAS project markers"
  if [ "$PAS_UPDATE_REPO" = "1" ]; then
    origin=$(git -C "$PAS_HOME" remote get-url origin 2>/dev/null) \
      || fatal "$PAS_HOME has no origin remote; set PAS_UPDATE_REPO=0 only for a deliberately pre-positioned checkout"
    [ "$(normalize_repo_url "$origin")" = "$(normalize_repo_url "$PAS_REPO")" ] \
      || fatal "$PAS_HOME origin does not match PAS_REPO"
    [ -z "$(checkout_changes)" ] \
      || fatal "$PAS_HOME repository has uncommitted changes"
  fi
}

tcp_preflight() {
  log "checking database TCP connectivity to $POLARDB_HOST:$POLARDB_PORT"
  if command -v python3 >/dev/null; then
    python3 - "$POLARDB_HOST" "$POLARDB_PORT" <<'PY' \
      || fatal "cannot reach the database endpoint; check endpoint, VPC, and whitelist"
import socket
import sys

socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
PY
  elif command -v timeout >/dev/null; then
    POLARDB_TCP_HOST="$POLARDB_HOST" POLARDB_TCP_PORT="$POLARDB_PORT" \
      timeout 5 bash -c 'exec 3<>"/dev/tcp/$POLARDB_TCP_HOST/$POLARDB_TCP_PORT"' \
      || fatal "cannot reach the database endpoint; check endpoint, VPC, and whitelist"
  else
    fatal "python3 or timeout is required for the TCP preflight"
  fi
}

resolve_docker() {
  if docker info >/dev/null 2>&1; then
    DOCKER_COMMAND=(docker)
  elif [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null \
    && sudo docker info >/dev/null 2>&1; then
    SUDO=(sudo)
    DOCKER_COMMAND=(sudo docker)
  else
    fatal "Docker daemon is not running or is not accessible"
  fi
  "${DOCKER_COMMAND[@]}" compose version >/dev/null 2>&1 \
    || fatal "Docker Compose v2 is required"
}

verify_image_architecture() {
  local image="$1" host_arch image_arch
  host_arch=$("${DOCKER_COMMAND[@]}" version --format '{{.Server.Arch}}')
  image_arch=$("${DOCKER_COMMAND[@]}" image inspect --format "{{.Architecture}}" "$image")
  [ "$image_arch" = "$host_arch" ] \
    || fatal "image architecture $image_arch does not match Docker host architecture $host_arch; set PAS_IMAGE to a matching image"
}

compose() {
  env -u PAS_DATABASE_URL -u PAS_ENCRYPTION_KEY -u PAS_PORT -u PAS_IMAGE \
    "${DOCKER_COMMAND[@]}" compose \
    -p "$PAS_COMPOSE_PROJECT" \
    --env-file "$SECRETS_DIR/pas-compose.env" \
    -f "$COMPOSE_FILE" "$@"
}

load_password() {
  if [ -n "${POLARDB_PASSWORD_FILE:-}" ]; then
    [ -f "$POLARDB_PASSWORD_FILE" ] || fatal "POLARDB_PASSWORD_FILE is not a regular file"
    local mode
    mode=$(stat -c '%a' "$POLARDB_PASSWORD_FILE")
    [ "${mode: -2}" = "00" ] || fatal "POLARDB_PASSWORD_FILE must not be readable by group or others"
    POLARDB_PASSWORD=$(<"$POLARDB_PASSWORD_FILE")
  elif [ -n "${POLARDB_PASSWORD:-}" ]; then
    POLARDB_PASSWORD="$POLARDB_PASSWORD"
  elif [ -t 0 ]; then
    read -r -s -p "PolarDB password: " POLARDB_PASSWORD
    printf '\n' >&2
  else
    fatal "set POLARDB_PASSWORD_FILE or run from an interactive target terminal"
  fi
  [ -n "$POLARDB_PASSWORD" ] || fatal "database password is empty"
  case "$POLARDB_PASSWORD" in
    *$'\n'*|*$'\r'*) fatal "database password must be single-line" ;;
  esac
  export -n POLARDB_PASSWORD POLARDB_PASSWORD_FILE 2>/dev/null || true
  unset POLARDB_PASSWORD_FILE
}

validate_target() {
  validate_inputs
  tcp_preflight
  if [ -e "$PAS_HOME" ] && [ ! -d "$PAS_HOME/.git" ] \
    && [ -n "$(ls -A "$PAS_HOME" 2>/dev/null)" ]; then
    fatal "$PAS_HOME is non-empty and is not a Git checkout"
  fi
  if [ -d "$PAS_HOME/.git" ]; then
    verify_checkout_identity
  fi
  if command -v docker >/dev/null; then
    resolve_docker
    log "Docker daemon and Compose v2 are available"
  elif command -v dnf >/dev/null || command -v yum >/dev/null \
    || command -v apt-get >/dev/null; then
    log "Docker is absent and will be installed during deployment"
  else
    fatal "Docker is absent and no supported package manager is available"
  fi
  log "validation passed: Linux target, inputs, database TCP path, Docker path, PAS_HOME, and repository identity"
}

prepare_home() {
  if [ ! -e "$PAS_HOME" ]; then
    if ! install -d -m 0755 "$PAS_HOME" 2>/dev/null; then
      require_sudo
      "${SUDO[@]}" install -d -m 0755 -o "$(id -u)" -g "$(id -g)" "$PAS_HOME"
    fi
  fi
  [ -d "$PAS_HOME" ] && [ -w "$PAS_HOME" ] \
    || fatal "$PAS_HOME must be a writable directory for the invoking user"
}

checkout_release() {
  prepare_home
  if [ -d "$PAS_HOME/.git" ]; then
    verify_checkout_identity
    if [ "$PAS_UPDATE_REPO" = "1" ]; then
      log "checking out PAS $PAS_REF in $PAS_HOME"
      git -C "$PAS_HOME" fetch --depth 1 origin "$PAS_REF" \
        || fatal "repository fetch failed for PAS_REF=$PAS_REF"
      git -C "$PAS_HOME" checkout --detach FETCH_HEAD \
        || fatal "repository checkout failed for PAS_REF=$PAS_REF"
    else
      log "using deliberately pre-positioned repository without fetching or checkout"
    fi
  else
    log "cloning PAS $PAS_REF into $PAS_HOME"
    git -C "$PAS_HOME" init -q
    git -C "$PAS_HOME" remote add origin "$PAS_REPO"
    git -C "$PAS_HOME" fetch --depth 1 origin "$PAS_REF" \
      || fatal "repository fetch failed for PAS_REF=$PAS_REF"
    git -C "$PAS_HOME" checkout --detach FETCH_HEAD \
      || fatal "repository checkout failed for PAS_REF=$PAS_REF"
  fi
  verify_checkout_identity
}

validate_target
if [ "$VALIDATE_ONLY" -eq 1 ]; then
  log "validate-only completed; no files, packages, images, containers, or services were changed"
  exit 0
fi
load_password
trap 'unset POLARDB_PASSWORD ENCODED_PASSWORD ENCRYPTION_KEY; [ -z "${PASSWORD_TMP:-}" ] || rm -f "$PASSWORD_TMP"' EXIT

command -v curl >/dev/null || pkg_install curl
command -v git >/dev/null || pkg_install git
command -v python3 >/dev/null || pkg_install python3
if ! command -v docker >/dev/null; then
  log "installing Docker from the configured operating-system repositories"
  if command -v dnf >/dev/null || command -v yum >/dev/null; then
    pkg_install docker docker-compose-plugin
  else
    pkg_install docker.io docker-compose-v2 || pkg_install docker.io docker-compose-plugin
  fi
  require_sudo
  "${SUDO[@]}" systemctl enable --now docker 2>/dev/null \
    || "${SUDO[@]}" service docker start 2>/dev/null \
    || fatal "Docker was installed but could not be started"
fi
resolve_docker

checkout_release
cd "$PAS_HOME"
[ -f "$COMPOSE_FILE" ] || fatal "$COMPOSE_FILE is missing from the repository"

EFFECTIVE_IMAGE="$PAS_IMAGE"
if "${DOCKER_COMMAND[@]}" image inspect "$PAS_IMAGE" >/dev/null 2>&1; then
  verify_image_architecture "$PAS_IMAGE"
  log "using existing local image $PAS_IMAGE"
elif command -v timeout >/dev/null \
  && timeout 150 "${DOCKER_COMMAND[@]}" pull \
    --platform "linux/$("${DOCKER_COMMAND[@]}" version --format '{{.Server.Arch}}')" \
    "$PAS_IMAGE"; then
  verify_image_architecture "$PAS_IMAGE"
  log "pulled $PAS_IMAGE"
else
  if [ "$PAS_ALLOW_LOCAL_BUILD" != "1" ]; then
    fatal "image pull failed and local build fallback is disabled; set PAS_IMAGE to an approved image or explicitly set PAS_ALLOW_LOCAL_BUILD=1"
  fi
  LOCAL_TAG="pas-local:${PAS_IMAGE##*:}"
  log "image pull failed; building $LOCAL_TAG from the verified checkout"
  DOCKER_BUILDKIT=1 "${DOCKER_COMMAND[@]}" build \
    --build-arg DEBIAN_MIRROR="$DEBIAN_MIRROR" \
    --build-arg DEBIAN_SECURITY_MIRROR="$DEBIAN_SECURITY_MIRROR" \
    --build-arg PYPI_INDEX_URL="$PYPI_INDEX" \
    -t "$LOCAL_TAG" .
  EFFECTIVE_IMAGE="$LOCAL_TAG"
  verify_image_architecture "$EFFECTIVE_IMAGE"
fi

mkdir -p "$SECRETS_DIR"
if [ ! -f "$SECRETS_DIR/pas_encryption_key" ]; then
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' \
    > "$SECRETS_DIR/pas_encryption_key"
  chmod 600 "$SECRETS_DIR/pas_encryption_key"
fi
ENCRYPTION_KEY=$(tr -d '\n' < "$SECRETS_DIR/pas_encryption_key")

PASSWORD_TMP=$(mktemp "$SECRETS_DIR/.polardb-password.XXXXXX")
printf '%s' "$POLARDB_PASSWORD" > "$PASSWORD_TMP"
log "ensuring metadata database $PAS_DB_NAME exists"
"${DOCKER_COMMAND[@]}" run --rm -i --network host --user 0:0 \
  --entrypoint python \
  --mount "type=bind,source=$PASSWORD_TMP,target=/run/secrets/polardb-password,readonly" \
  -e POLARDB_HOST="$POLARDB_HOST" \
  -e POLARDB_PORT="$POLARDB_PORT" \
  -e POLARDB_USER="$POLARDB_USER" \
  -e PAS_DB_NAME="$PAS_DB_NAME" \
  "$EFFECTIVE_IMAGE" - <<'PY'
import asyncio
import os

import asyncmy


async def main():
    with open("/run/secrets/polardb-password", encoding="utf-8") as stream:
        password = stream.read()
    connection = await asyncmy.connect(
        host=os.environ["POLARDB_HOST"],
        port=int(os.environ["POLARDB_PORT"]),
        user=os.environ["POLARDB_USER"],
        password=password,
    )
    try:
        async with connection.cursor() as cursor:
            database = os.environ["PAS_DB_NAME"]
            await cursor.execute(
                "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME=%s",
                (database,),
            )
            if await cursor.fetchone() is None:
                await cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    "DEFAULT CHARACTER SET utf8mb4"
                )
    finally:
        connection.close()


asyncio.run(main())
PY
rm -f "$PASSWORD_TMP"
PASSWORD_TMP=""

ENCODED_USER=$(printf '%s' "$POLARDB_USER" | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
ENCODED_PASSWORD=$(printf '%s' "$POLARDB_PASSWORD" | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
cat > "$SECRETS_DIR/pas-compose.env" <<EOF
PAS_DATABASE_URL=mysql+asyncmy://$ENCODED_USER:$ENCODED_PASSWORD@$POLARDB_HOST:$POLARDB_PORT/$PAS_DB_NAME
PAS_ENCRYPTION_KEY=$ENCRYPTION_KEY
PAS_PORT=$PAS_PORT
PAS_IMAGE=$EFFECTIVE_IMAGE
EOF
chmod 600 "$SECRETS_DIR/pas-compose.env"
unset POLARDB_PASSWORD ENCODED_PASSWORD ENCRYPTION_KEY

log "starting Compose project $PAS_COMPOSE_PROJECT"
compose config --quiet
compose up -d

log "waiting for /readyz on port $PAS_PORT"
READY=0
for attempt in $(seq 1 90); do
  if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$PAS_PORT/readyz" 2>/dev/null; then
    READY=1
    break
  fi
  sleep 2
done
if [ "$READY" -ne 1 ]; then
  compose logs --tail=50 migrate server >&2 || true
  fatal "server did not become ready within 180 seconds"
fi

READY_JSON=$(curl -fsS "http://127.0.0.1:$PAS_PORT/readyz")
PAS_MODE=$(printf '%s' "$READY_JSON" | python3 -c \
  'import json, sys; print(json.load(sys.stdin).get("mode", "UNKNOWN"))')
if [ "$PAS_MODE" = "SETUP" ]; then
  TOKEN_CONTAINER_PATH="/var/run/pas/bootstrap-token.$$.txt"
  rm -f "$SECRETS_DIR/bootstrap_token.txt"
  compose exec -T server sh -c \
    'rm -f "$1"; pas config bootstrap-token issue --output "$1" >/dev/null' \
    sh "$TOKEN_CONTAINER_PATH"
  SERVER_CONTAINER=$(compose ps -q server)
  [ -n "$SERVER_CONTAINER" ] || fatal "cannot resolve the PAS server container"
  "${DOCKER_COMMAND[@]}" cp \
    "$SERVER_CONTAINER:$TOKEN_CONTAINER_PATH" \
    "$SECRETS_DIR/bootstrap_token.txt" >/dev/null
  compose exec -T server rm -f "$TOKEN_CONTAINER_PATH"
  chmod 600 "$SECRETS_DIR/bootstrap_token.txt"
  TOKEN_RESULT="Bootstrap token stored at $SECRETS_DIR/bootstrap_token.txt (mode 0600); read it directly on the target host."
else
  TOKEN_RESULT="PAS mode is $PAS_MODE; no bootstrap token was issued."
fi

PRIVATE_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
ACCESS_IP="${PRIVATE_IP:-127.0.0.1}"
printf '\nPAS deployed successfully with Docker Compose.\n'
printf 'Console/API/MCP: http://%s:%s (MCP endpoint /mcp)\n' "$ACCESS_IP" "$PAS_PORT"
printf 'Image: %s\n' "$EFFECTIVE_IMAGE"
printf '%s\n' "$TOKEN_RESULT"
printf 'Review security-group rules separately and restrict inbound sources.\n'
