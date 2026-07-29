#!/usr/bin/env python3
"""Update every explicit release-version location atomically."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from versioning import (
    VERSION,
    planned_replacements,
    read_versions,
    verify_versions,
)


def _require_clean(root: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        raise ValueError("worktree must be clean")


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if not VERSION.fullmatch(args.version):
            raise ValueError("version must use MAJOR.MINOR.PATCH")
        _require_clean(root)
        discovered = read_versions(root)
        old_versions = set(discovered.values())
        if len(old_versions) != 1:
            raise ValueError("current release versions do not agree")
        old = old_versions.pop()
        verify_versions(root, old)
        if old == args.version:
            raise ValueError(f"version is already {args.version}")
        replacements = planned_replacements(
            root,
            old,
            args.version,
        )
        for path, content in replacements.items():
            _atomic_write(path, content)
        verify_versions(root, args.version)
        for path in sorted(replacements):
            print(path.relative_to(root))
        return 0
    except (
        KeyError,
        OSError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"version bump failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
