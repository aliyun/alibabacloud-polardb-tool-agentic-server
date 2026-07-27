#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

uv run python -c '
from alembic.config import Config
from alembic.script import ScriptDirectory

heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
if len(heads) != 1:
    raise SystemExit(f"expected exactly one Alembic head, found {len(heads)}: {heads}")
print(f"Alembic head: {heads[0]}")
'
