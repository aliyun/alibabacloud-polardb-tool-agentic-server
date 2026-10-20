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

if [ "${PAS_TEST_SCALABILITY_DATABASE_OK:-0}" = "1" ]; then
  PAS_TEST_SCALABILITY_DATABASE_URL=$PAS_DATABASE_URL
  export PAS_TEST_SCALABILITY_DATABASE_URL
  case "$MODE" in
    mysql)
      PAS_TEST_MYSQL_URL=$PAS_DATABASE_URL
      PAS_TEST_MYSQL_DATABASE_OK=1
      export PAS_TEST_MYSQL_URL PAS_TEST_MYSQL_DATABASE_OK
      uv run pytest tests/test_configuration_cross_backend.py tests/test_knowledge_backends.py -k MYSQL_URL -q
      ;;
    postgresql)
      PAS_TEST_POSTGRES_URL=$PAS_DATABASE_URL
      PAS_TEST_POSTGRES_SCHEMA_OK=1
      export PAS_TEST_POSTGRES_URL PAS_TEST_POSTGRES_SCHEMA_OK
      uv run pytest tests/test_configuration_cross_backend.py tests/test_knowledge_backends.py -k POSTGRES_URL -q
      ;;
  esac
  uv run pytest tests/integration/test_pas_scalability_backends.py -q
  if [ "$MODE" = "mysql" ]; then
    uv run pytest tests/test_management_accounts_mysql.py -q
    case "$PAS_DATABASE_URL" in
      mysql+asyncmy://root@127.0.0.1:*/pas_local_reg_*)
        uv run pytest tests/integration/test_env_generator_native_mysql.py -q
        ;;
    esac
  fi
  if [ "$MODE" = "postgresql" ]; then
    PAS_TEST_POSTGRES_URL=$PAS_DATABASE_URL
    PAS_TEST_POSTGRES_SCHEMA_OK=1
    export PAS_TEST_POSTGRES_URL PAS_TEST_POSTGRES_SCHEMA_OK
    uv run pytest tests/test_provisioning_capacity_postgres.py tests/test_password_session_races_postgres.py \
      tests/test_credential_mutation.py::test_concurrent_reset_rejects_stale_inflight_self_change -q
  fi
fi
