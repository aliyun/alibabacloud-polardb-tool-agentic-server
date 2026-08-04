#!/usr/bin/env bash
# Deploy PolarDB Tool Agentic Server from source on the target Linux host.
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
PAS_VERSION="${PAS_VERSION:-0.0.5}"
PAS_REF="${PAS_REF:-v${PAS_VERSION}}"
PAS_UPDATE_REPO="${PAS_UPDATE_REPO:-1}"
SKIP_WEB="${SKIP_WEB:-0}"
PYPI_INDEX="${PYPI_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
UV_VERSION="${UV_VERSION:-0.7.20}"

BACKEND_PORT=18760
WEB_PORT=18761
SECRETS_DIR="$PAS_HOME/.secrets"
RUN_DIR="$PAS_HOME/run"
PATH="${HOME}/.local/bin:$PATH"
export PATH

log() { printf '[deploy] %s\n' "$*"; }
fatal() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

SUDO=()

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
  case "$POLARDB_PORT" in
    ''|*[!0-9]*) fatal "POLARDB_PORT must be an integer" ;;
  esac
  [ "$POLARDB_PORT" -ge 1 ] && [ "$POLARDB_PORT" -le 65535 ] \
    || fatal "POLARDB_PORT must be between 1 and 65535"
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
  case "$PAS_HOME" in
    /*) ;;
    *) fatal "PAS_HOME must be an absolute path" ;;
  esac
  [ "$PAS_HOME" != "/" ] || fatal "PAS_HOME cannot be /"
  [ "$SKIP_WEB" = "0" ] || [ "$SKIP_WEB" = "1" ] \
    || fatal "SKIP_WEB must be 0 or 1"
  [ "$PAS_UPDATE_REPO" = "0" ] || [ "$PAS_UPDATE_REPO" = "1" ] \
    || fatal "PAS_UPDATE_REPO must be 0 or 1"
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
  log "validation passed: Linux target, inputs, database TCP path, PAS_HOME, and repository identity"
}

prepare_home() {
  [ -e "$PAS_HOME" ] && return 0
  if ! install -d -m 0755 "$PAS_HOME" 2>/dev/null; then
    require_sudo
    "${SUDO[@]}" install -d -m 0755 -o "$(id -u)" -g "$(id -g)" "$PAS_HOME"
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
  trap 'unset POLARDB_PASSWORD ENCODED_PASSWORD PAS_DATABASE_URL_VALUE PAS_ENCRYPTION_KEY_VALUE' EXIT
}

wait_http() {
  local url="$1" service="$2" pid_file="$3" log_file="$4"
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
      return 0
    fi
    if ! kill -0 "$(<"$pid_file")" 2>/dev/null; then
      tail -50 "$log_file" >&2 || true
      fatal "$service exited during startup"
    fi
    sleep 1
  done
  tail -50 "$log_file" >&2 || true
  fatal "$service did not become ready within 60 seconds"
}

stop_managed_process() {
  local pid_file="$1" expected_cwd="$2" expected_command="$3"
  [ -f "$pid_file" ] || return 0
  local pid cwd command_line attempt
  pid=$(<"$pid_file")
  case "$pid" in ''|*[!0-9]*) fatal "invalid PID file: $pid_file" ;; esac
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    return 0
  fi
  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
  command_line=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  [ "$cwd" = "$expected_cwd" ] && [[ "$command_line" == *"$expected_command"* ]] \
    || fatal "refusing to stop PID $pid because it is not the managed PAS process"
  kill "$pid"
  for attempt in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || { rm -f "$pid_file"; return 0; }
    sleep 1
  done
  fatal "managed process $pid did not stop after SIGTERM"
}

validate_target
if [ "$VALIDATE_ONLY" -eq 1 ]; then
  log "validate-only completed; no files, packages, processes, or services were changed"
  exit 0
fi
load_password

command -v curl >/dev/null || pkg_install curl
command -v git >/dev/null || pkg_install git
command -v python3 >/dev/null || pkg_install python3 python3-pip
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
  || fatal "Python 3.11 or newer is required"
python3 -m pip --version >/dev/null 2>&1 || pkg_install python3-pip
if ! command -v uv >/dev/null; then
  log "installing pinned uv $UV_VERSION"
  python3 -m pip install --user "uv==$UV_VERSION" --index-url "$PYPI_INDEX"
fi
command -v uv >/dev/null || fatal "uv was installed but is not on PATH"

if [ "$SKIP_WEB" = "0" ]; then
  if ! command -v npm >/dev/null; then
    pkg_install nodejs npm
  fi
  node_major=$(node -e 'process.stdout.write(String(parseInt(process.versions.node)))' 2>/dev/null || printf '0')
  [ "$node_major" -ge 20 ] \
    || fatal "Node.js 20 or newer is required; use SKIP_WEB=1 for backend-only deployment"
fi

checkout_release
cd "$PAS_HOME"

mkdir -p "$SECRETS_DIR" "$RUN_DIR" "$PAS_HOME/data"
LOCKED_REQUIREMENTS="$RUN_DIR/requirements.locked.txt"
log "exporting the frozen lock and installing hash-verified dependencies"
uv export --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file "$LOCKED_REQUIREMENTS" >/dev/null
uv venv --python "$(command -v python3)" "$PAS_HOME/.venv"
UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-60}" uv pip sync \
  --python "$PAS_HOME/.venv/bin/python" \
  --require-hashes --index-url "$PYPI_INDEX" --link-mode copy \
  "$LOCKED_REQUIREMENTS"
UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-60}" uv pip install \
  --python "$PAS_HOME/.venv/bin/python" \
  --index-url "$PYPI_INDEX" --link-mode copy --no-deps --reinstall .

if [ ! -f "$SECRETS_DIR/pas_encryption_key" ]; then
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' \
    > "$SECRETS_DIR/pas_encryption_key"
  chmod 600 "$SECRETS_DIR/pas_encryption_key"
fi

log "ensuring metadata database $PAS_DB_NAME exists"
POLARDB_HOST="$POLARDB_HOST" POLARDB_PORT="$POLARDB_PORT" \
POLARDB_USER="$POLARDB_USER" POLARDB_PASSWORD="$POLARDB_PASSWORD" \
PAS_DB_NAME="$PAS_DB_NAME" "$PAS_HOME/.venv/bin/python" - <<'PY'
import asyncio
import os

import asyncmy


async def main():
    connection = await asyncmy.connect(
        host=os.environ["POLARDB_HOST"],
        port=int(os.environ["POLARDB_PORT"]),
        user=os.environ["POLARDB_USER"],
        password=os.environ["POLARDB_PASSWORD"],
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

ENCODED_USER=$(printf '%s' "$POLARDB_USER" | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
ENCODED_PASSWORD=$(printf '%s' "$POLARDB_PASSWORD" | python3 -c \
  'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
PAS_DATABASE_URL_VALUE="mysql+asyncmy://$ENCODED_USER:$ENCODED_PASSWORD@$POLARDB_HOST:$POLARDB_PORT/$PAS_DB_NAME"
PAS_ENCRYPTION_KEY_VALUE="file:$SECRETS_DIR/pas_encryption_key"
cat > "$SECRETS_DIR/pas.env" <<EOF
PAS_DATABASE_URL=$PAS_DATABASE_URL_VALUE
PAS_ENCRYPTION_KEY=$PAS_ENCRYPTION_KEY_VALUE
EOF
chmod 600 "$SECRETS_DIR/pas.env"
unset POLARDB_PASSWORD ENCODED_PASSWORD

log "running database migrations"
PAS_DATABASE_URL="$PAS_DATABASE_URL_VALUE" \
PAS_ENCRYPTION_KEY="$PAS_ENCRYPTION_KEY_VALUE" \
  "$PAS_HOME/.venv/bin/alembic" upgrade head

log "starting backend on port $BACKEND_PORT"
stop_managed_process "$RUN_DIR/backend.pid" "$PAS_HOME" "python -m server"
curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$BACKEND_PORT/readyz" 2>/dev/null \
  && fatal "port $BACKEND_PORT already serves an unmanaged PAS instance"
nohup env PAS_DATABASE_URL="$PAS_DATABASE_URL_VALUE" \
  PAS_ENCRYPTION_KEY="$PAS_ENCRYPTION_KEY_VALUE" \
  "$PAS_HOME/.venv/bin/python" -m server > "$RUN_DIR/backend.out" 2>&1 &
printf '%s\n' "$!" > "$RUN_DIR/backend.pid"
wait_http "http://127.0.0.1:$BACKEND_PORT/readyz" \
  backend "$RUN_DIR/backend.pid" "$RUN_DIR/backend.out"

if [ "$SKIP_WEB" = "0" ]; then
  log "starting web console on port $WEB_PORT"
  cd "$PAS_HOME/web"
  npm ci --registry="$NPM_REGISTRY"
  stop_managed_process "$RUN_DIR/web.pid" "$PAS_HOME/web" "npm run dev"
  curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$WEB_PORT/" 2>/dev/null \
    && fatal "port $WEB_PORT already serves an unmanaged web process"
  nohup npm run dev -- --host 0.0.0.0 > "$RUN_DIR/web.out" 2>&1 &
  printf '%s\n' "$!" > "$RUN_DIR/web.pid"
  wait_http "http://127.0.0.1:$WEB_PORT/" \
    web-console "$RUN_DIR/web.pid" "$RUN_DIR/web.out"
  cd "$PAS_HOME"
fi

READY_JSON=$(curl -fsS "http://127.0.0.1:$BACKEND_PORT/readyz")
PAS_MODE=$(printf '%s' "$READY_JSON" | python3 -c \
  'import json, sys; print(json.load(sys.stdin).get("mode", "UNKNOWN"))')
if [ "$PAS_MODE" = "SETUP" ]; then
  rm -f "$SECRETS_DIR/bootstrap_token.txt"
  if ! PAS_DATABASE_URL="$PAS_DATABASE_URL_VALUE" \
    PAS_ENCRYPTION_KEY="$PAS_ENCRYPTION_KEY_VALUE" \
    "$PAS_HOME/.venv/bin/pas" config bootstrap-token issue \
      --output "$SECRETS_DIR/bootstrap_token.txt" \
      >"$RUN_DIR/bootstrap-token.out" 2>&1; then
    tail -50 "$RUN_DIR/bootstrap-token.out" >&2 || true
    fatal "bootstrap token issuance failed"
  fi
  chmod 600 "$SECRETS_DIR/bootstrap_token.txt"
  TOKEN_RESULT="Bootstrap token stored at $SECRETS_DIR/bootstrap_token.txt (mode 0600); read it directly on the target host."
else
  TOKEN_RESULT="PAS mode is $PAS_MODE; no bootstrap token was issued."
fi

PRIVATE_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
ACCESS_IP="${PRIVATE_IP:-127.0.0.1}"
printf '\nPAS deployed successfully from source.\n'
printf 'Backend/MCP: http://%s:%s (MCP endpoint /mcp)\n' "$ACCESS_IP" "$BACKEND_PORT"
if [ "$SKIP_WEB" = "0" ]; then
  printf 'Web console: http://%s:%s\n' "$ACCESS_IP" "$WEB_PORT"
fi
printf '%s\n' "$TOKEN_RESULT"
printf 'Review security-group rules separately and restrict inbound sources.\n'
