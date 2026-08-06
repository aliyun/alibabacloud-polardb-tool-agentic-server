#!/bin/sh

set -eu

DEFAULT_PAS_IMAGE=ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:0.0.6

output=.env
selected_image=$DEFAULT_PAS_IMAGE
explicit_image=false
skip_connection_test=false
temporary_directory=

usage() {
  echo "Usage: $0 [--output PATH] [--image IMAGE] [--skip-connection-test]"
}

fail() {
  echo "Error: $1" >&2
  exit 1
}

cleanup() {
  if [ -n "$temporary_directory" ] && [ -d "$temporary_directory" ]; then
    rm -rf "$temporary_directory"
  fi
}

trap cleanup 0
trap 'exit 1' 1 2 15

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      [ "$#" -ge 2 ] || fail "--output requires a path."
      output=$2
      shift 2
      ;;
    --image)
      [ "$#" -ge 2 ] || fail "--image requires an image reference."
      selected_image=$2
      explicit_image=true
      shift 2
      ;;
    --skip-connection-test)
      skip_connection_test=true
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown option: $1"
      ;;
  esac
done

[ -n "$output" ] || fail "The output path cannot be empty."
[ -n "$selected_image" ] || fail "The image reference cannot be empty."
[ -t 0 ] && [ -t 1 ] || fail "An interactive terminal is required."

output_directory=$(dirname "$output")
output_name=$(basename "$output")
[ -d "$output_directory" ] || fail "The output directory does not exist."
output_directory=$(cd "$output_directory" && pwd -P)
output=$output_directory/$output_name

if [ -e "$output" ] || [ -L "$output" ]; then
  fail "The output file already exists: $output"
fi

temporary_directory=$(mktemp -d "$output_directory/.pas-env.XXXXXX")
chmod 0700 "$temporary_directory"

set -- docker run --rm --interactive --tty --pull=missing \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$temporary_directory,dst=/output" \
  --entrypoint pas \
  "$selected_image" \
  database create-env --output /output/generated.env

if [ "$explicit_image" = true ]; then
  set -- "$@" --image "$selected_image"
fi
if [ "$skip_connection_test" = true ]; then
  set -- "$@" --skip-connection-test
fi

"$@"

generated=$temporary_directory/generated.env
if [ ! -f "$generated" ] || [ -L "$generated" ]; then
  fail "The generator did not create a regular environment file."
fi

generated_owner=$(ls -ldn "$generated" | awk '{print $3}')
[ "$generated_owner" = "$(id -u)" ] ||
  fail "The generated environment file has an unexpected owner."

chmod 0600 "$generated"
ln "$generated" "$output"
unlink "$generated"

echo "Environment file created at $output"
