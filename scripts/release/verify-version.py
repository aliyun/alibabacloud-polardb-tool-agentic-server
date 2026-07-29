#!/usr/bin/env python3
"""Verify that a release tag matches every versioned project component."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from versioning import verify_versions


SEMVER_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def verify(tag: str, root: Path) -> str:
    match = SEMVER_TAG.fullmatch(tag)
    if not match:
        raise ValueError("release tag must use vMAJOR.MINOR.PATCH")
    version = tag.removeprefix("v")
    verify_versions(root, version)
    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        print(verify(args.tag, args.root.resolve()))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"version verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
