#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=
TAG=
REPORT=
ARCHIVE=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPO=$2; shift 2 ;;
    --tag) TAG=$2; shift 2 ;;
    --report) REPORT=$2; shift 2 ;;
    --archive) ARCHIVE=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$REPO:$REPORT" in
  /*:/*) ;;
  *) echo "--repo and --report must be absolute paths" >&2; exit 2 ;;
esac
if [ -z "$TAG" ]; then
  echo "--tag is required" >&2
  exit 2
fi

fail() {
  echo "public ref audit failed: $1" >&2
  exit 1
}

test "$(git -C "$REPO" branch --show-current)" = "main" || fail "current branch is not main"
BRANCHES=$(git -C "$REPO" for-each-ref --format='%(refname:short)' refs/heads)
test "$BRANCHES" = "main" || fail "unexpected local branch refs"
TAGS=$(git -C "$REPO" for-each-ref --format='%(refname:short)' refs/tags)
test "$TAGS" = "$TAG" || fail "unexpected tag refs"
test "$(git -C "$REPO" rev-list --all --count)" = "1" || fail "history is not a single commit"
test "$(git -C "$REPO" rev-list --max-parents=0 --count HEAD)" = "1" || fail "root commit count is not one"
test "$(git -C "$REPO" rev-parse HEAD)" = "$(git -C "$REPO" rev-parse "$TAG^{commit}")" \
  || fail "tag does not identify main HEAD"
test -z "$(git -C "$REPO" status --porcelain)" || fail "candidate worktree is dirty"

FSCK_OUTPUT=$(git -C "$REPO" fsck --full --no-reflogs --unreachable 2>/dev/null)
test -z "$FSCK_OUTPUT" || fail "unreachable Git objects remain"

python3 "$SCRIPT_DIR/scan.py" scan --root "$REPO" || fail "tree content scan failed"

if [ -n "$ARCHIVE" ]; then
  case "$ARCHIVE" in
    /*) ;;
    *) fail "archive path is not absolute" ;;
  esac
  python3 - "$REPO" "$ARCHIVE" <<'PY'
import sys
import tarfile
from pathlib import Path, PurePosixPath

repo = Path(sys.argv[1])
archive_path = Path(sys.argv[2])
with tarfile.open(archive_path, "r:gz") as archive:
    files = []
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise SystemExit("unsafe source archive member")
        if member.isfile():
            if len(path.parts) < 2:
                raise SystemExit("source archive is missing its release prefix")
            files.append(PurePosixPath(*path.parts[1:]).as_posix())

import subprocess

tracked = subprocess.run(
    ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()
if sorted(files) != sorted(tracked):
    raise SystemExit("source archive does not match the committed tree")
PY
fi

mkdir -p "$(dirname -- "$REPORT")"
COMMIT=$(git -C "$REPO" rev-parse HEAD)
TREE_FILES=$(git -C "$REPO" ls-tree -r --name-only HEAD | wc -l | tr -d ' ')
python3 - "$REPORT" "$COMMIT" "$TAG" "$TREE_FILES" <<'PY'
import json
import sys
from pathlib import Path

report = {
    "schema_version": 1,
    "status": "passed",
    "branch": "main",
    "commit": sys.argv[2],
    "commit_count": 1,
    "tag": sys.argv[3],
    "tree_file_count": int(sys.argv[4]),
    "checks": [
        "single_root_commit",
        "exact_refs",
        "clean_worktree",
        "no_unreachable_objects",
        "sanitized_tree",
        "source_archive",
    ],
}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
