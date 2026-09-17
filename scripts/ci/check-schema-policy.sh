#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

check_file() {
    file=$1
    python3 "$ROOT/scripts/ci/check_schema_policy.py" "$file"
}

if [ "$#" -gt 0 ]; then
    for file in "$@"; do
        check_file "$file"
    done
    exit 0
fi

base=${SCHEMA_POLICY_BASE:-}
if [ -n "$base" ]; then
    files=$(git diff --name-only --diff-filter=A "$base"...HEAD -- server/db/migrations/versions)
else
    files=$(git diff --name-only --diff-filter=A HEAD -- server/db/migrations/versions)
fi

untracked=$(git ls-files --others --exclude-standard -- server/db/migrations/versions)
files=$(printf '%s\n%s\n' "$files" "$untracked" | sed '/^$/d' | sort -u)

for file in $files; do
    check_file "$file"
done

echo "Schema policy check passed."
