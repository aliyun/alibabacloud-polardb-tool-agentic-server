#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: build-deployment-bundle.sh VERSION ABSOLUTE_OUTPUT_DIRECTORY" >&2
  exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=$1
OUTPUT_DIRECTORY=$2

python3 "$SCRIPT_DIR/check-assets.py" build-bundle \
  --version "$VERSION" \
  --output-directory "$OUTPUT_DIRECTORY"
