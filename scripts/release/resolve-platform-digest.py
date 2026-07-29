#!/usr/bin/env python3
"""Resolve one immutable platform digest from an OCI image index."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}


def resolve_platform_digest(
    index: Mapping[str, Any],
    os_name: str,
    architecture: str,
) -> str:
    if index.get("mediaType") not in INDEX_MEDIA_TYPES:
        raise ValueError("manifest must be an OCI image index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ValueError("image index manifests must be a list")
    matches: list[Mapping[str, Any]] = []
    for descriptor in manifests:
        if not isinstance(descriptor, Mapping):
            raise ValueError("image index descriptor must be an object")
        platform = descriptor.get("platform")
        if not isinstance(platform, Mapping):
            continue
        if (
            platform.get("os") == os_name
            and platform.get("architecture") == architecture
        ):
            matches.append(descriptor)
    platform_name = f"{os_name}/{architecture}"
    if not matches:
        raise ValueError(f"missing platform descriptor: {platform_name}")
    if len(matches) != 1:
        raise ValueError(f"multiple platform descriptors: {platform_name}")
    digest = matches[0].get("digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError(f"invalid digest for platform: {platform_name}")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()
    try:
        if not args.manifest.is_absolute():
            raise ValueError("--manifest must be an absolute path")
        parts = args.platform.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("--platform must use OS/ARCHITECTURE")
        index = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(index, Mapping):
            raise ValueError("manifest must be a JSON object")
        print(resolve_platform_digest(index, parts[0], parts[1]))
        return 0
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"platform digest resolution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
