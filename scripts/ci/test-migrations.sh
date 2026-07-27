#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
MODE=${1:-}
cd "$ROOT"
PAS_ENCRYPTION_KEY=${PAS_ENCRYPTION_KEY:-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=}
export PAS_ENCRYPTION_KEY

case "$MODE" in
  sqlite)
    TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pas-migration-sqlite.XXXXXX")
    trap 'rm -rf "$TEMP_ROOT"' EXIT HUP INT TERM
    PAS_DATABASE_URL="sqlite+aiosqlite:///$TEMP_ROOT/fresh.db"
    export PAS_DATABASE_URL
    uv run pas database migrate
    uv run pas database check
    uv run pytest \
      tests/test_database_schema.py::test_migrate_database_initializes_fresh_sqlite \
      tests/test_database_schema.py::test_migrate_database_preserves_populated_pre_head_data
    ;;
  mysql)
    PAS_DATABASE_URL=${PAS_DATABASE_URL:-mysql+asyncmy://pas:pas-password@127.0.0.1:3306/pas}
    export PAS_DATABASE_URL
    uv run pas database migrate
    uv run pas database check
    ;;
  postgresql)
    PAS_DATABASE_URL=${PAS_DATABASE_URL:-postgresql+asyncpg://pas:pas-password@127.0.0.1:5432/pas}
    export PAS_DATABASE_URL
    uv run pas database migrate
    uv run pas database check
    ;;
  *)
    echo "usage: test-migrations.sh {sqlite|mysql|postgresql}" >&2
    exit 2
    ;;
esac
