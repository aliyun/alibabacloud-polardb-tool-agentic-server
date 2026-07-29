#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_SOURCE=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
SOURCE=$DEFAULT_SOURCE
REPORT=
VERSION=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      SOURCE=$2
      shift 2
      ;;
    --report)
      REPORT=$2
      shift 2
      ;;
    --version)
      VERSION=$2
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

SOURCE=$(CDPATH= cd -- "$SOURCE" && pwd)
python3 - "$VERSION" <<'PY' || {
import re
import sys

if re.fullmatch(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)",
    sys.argv[1],
) is None:
    raise SystemExit(1)
PY
  echo "--version must use MAJOR.MINOR.PATCH" >&2
  exit 2
}
if [ -z "$REPORT" ]; then
  REPORT=$DEFAULT_SOURCE/.public-release/v"$VERSION"-audit.json
fi
case "$REPORT" in
  /*) ;;
  *) echo "--report must be an absolute path" >&2; exit 2 ;;
esac
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pas-public-rehearsal.XXXXXX")
trap 'rm -rf "$TEMP_ROOT"' EXIT HUP INT TERM
PUBLIC_TREE=$TEMP_ROOT/public
REFS_BEFORE=$TEMP_ROOT/refs-before
REFS_AFTER=$TEMP_ROOT/refs-after

git -C "$SOURCE" show-ref >"$REFS_BEFORE" 2>/dev/null || true
"$SCRIPT_DIR/export.sh" --source "$SOURCE" --output "$PUBLIC_TREE"

git -C "$PUBLIC_TREE" init -q -b main
git -C "$PUBLIC_TREE" add .
git -C "$PUBLIC_TREE" \
  -c user.name="Public Release Rehearsal" \
  -c user.email="release-rehearsal@example.invalid" \
  commit -qm "release: public source snapshot"
git -C "$PUBLIC_TREE" tag v"$VERSION"

SOURCE_TAR=$TEMP_ROOT/polardb-agentic-server-"$VERSION"-source.tar
SOURCE_ARCHIVE=$SOURCE_TAR.gz
git -C "$PUBLIC_TREE" archive \
  --format=tar \
  --prefix=polardb-agentic-server-"$VERSION"/ \
  HEAD >"$SOURCE_TAR"
gzip -n -c "$SOURCE_TAR" >"$SOURCE_ARCHIVE"

"$SCRIPT_DIR/audit-refs.sh" \
  --repo "$PUBLIC_TREE" \
  --tag v"$VERSION" \
  --archive "$SOURCE_ARCHIVE" \
  --report "$REPORT"

git -C "$SOURCE" show-ref >"$REFS_AFTER" 2>/dev/null || true
cmp "$REFS_BEFORE" "$REFS_AFTER"
echo "one-root-commit: ok"
echo "audit-report: $REPORT"
