#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_SOURCE=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
SOURCE=$DEFAULT_SOURCE
OUTPUT=
CHECK=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      SOURCE=$2
      shift 2
      ;;
    --output)
      OUTPUT=$2
      shift 2
      ;;
    --check)
      CHECK=true
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ "$CHECK" = true ]; then
  if [ -n "$OUTPUT" ]; then
    echo "--check and --output cannot be combined" >&2
    exit 2
  fi
  TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pas-public-export.XXXXXX")
  trap 'rm -rf "$TEMP_ROOT"' EXIT HUP INT TERM
  OUTPUT=$TEMP_ROOT/tree
fi

if [ -z "$OUTPUT" ]; then
  echo "usage: export.sh [--source ABSOLUTE_DIR] (--output ABSOLUTE_DIR | --check)" >&2
  exit 2
fi

python3 "$SCRIPT_DIR/scan.py" export --source "$SOURCE" --output "$OUTPUT"
