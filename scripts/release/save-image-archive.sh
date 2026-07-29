#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
IMAGE=
PLATFORM=
VERSION=
REVISION=
OUTPUT=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --image) IMAGE=$2; shift 2 ;;
    --platform) PLATFORM=$2; shift 2 ;;
    --version) VERSION=$2; shift 2 ;;
    --revision) REVISION=$2; shift 2 ;;
    --output) OUTPUT=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$IMAGE" in
  *@sha256:*) ;;
  *) echo "--image must be an immutable digest reference" >&2; exit 2 ;;
esac
case "$PLATFORM" in
  linux/amd64|linux/arm64) ;;
  *) echo "--platform must be linux/amd64 or linux/arm64" >&2; exit 2 ;;
esac
if [ -z "$VERSION" ] || [ -z "$REVISION" ] || [ -z "$OUTPUT" ]; then
  echo "--version, --revision, and --output are required" >&2
  exit 2
fi
case "$OUTPUT" in
  /*) ;;
  *) echo "--output must be absolute" >&2; exit 2 ;;
esac

MANIFEST=$(mktemp "${TMPDIR:-/tmp}/pas-image-index.XXXXXX")
TEMP_TAR="${OUTPUT}.tmp.$$.tar"
TEMP_OUTPUT="${OUTPUT}.tmp.$$"
trap 'rm -f "$MANIFEST" "$TEMP_TAR" "$TEMP_OUTPUT"' EXIT HUP INT TERM
docker buildx imagetools inspect --raw "$IMAGE" >"$MANIFEST"
CHILD_DIGEST=$(
  python3 "$SCRIPT_DIR/resolve-platform-digest.py" \
    --manifest "$MANIFEST" \
    --platform "$PLATFORM"
)
REPOSITORY=${IMAGE%@*}
CHILD_IMAGE="$REPOSITORY@$CHILD_DIGEST"

docker pull --platform "$PLATFORM" "$CHILD_IMAGE"
ACTUAL_VERSION=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$CHILD_IMAGE")
ACTUAL_REVISION=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$CHILD_IMAGE")
if [ "$ACTUAL_VERSION" != "$VERSION" ] || [ "$ACTUAL_REVISION" != "$REVISION" ]; then
  echo "image release labels do not match expected version and revision" >&2
  exit 1
fi

docker save --output "$TEMP_TAR" "$CHILD_IMAGE"
gzip -n -c "$TEMP_TAR" >"$TEMP_OUTPUT"
mv "$TEMP_OUTPUT" "$OUTPUT"
echo "$PLATFORM=$CHILD_DIGEST"
rm -f "$MANIFEST" "$TEMP_TAR"
trap - EXIT HUP INT TERM
