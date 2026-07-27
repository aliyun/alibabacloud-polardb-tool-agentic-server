#!/bin/sh
set -eu

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

docker pull --platform "$PLATFORM" "$IMAGE"
ACTUAL_VERSION=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE")
ACTUAL_REVISION=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE")
if [ "$ACTUAL_VERSION" != "$VERSION" ] || [ "$ACTUAL_REVISION" != "$REVISION" ]; then
  echo "image release labels do not match expected version and revision" >&2
  exit 1
fi

TEMP_OUTPUT="${OUTPUT}.tmp.$$"
trap 'rm -f "$TEMP_OUTPUT"' EXIT HUP INT TERM
docker save "$IMAGE" | gzip -n >"$TEMP_OUTPUT"
mv "$TEMP_OUTPUT" "$OUTPUT"
trap - EXIT HUP INT TERM
