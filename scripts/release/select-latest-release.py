#!/usr/bin/env python3
"""Select a published semantic version without moving latest backward."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any


TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _version_tuple(tag: str) -> tuple[int, int, int]:
    match = TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"malformed release tag: {tag}")
    return tuple(int(part) for part in match.groups())


def highest_version(tags: Sequence[str]) -> str:
    if not tags:
        raise ValueError("no published semantic versions")
    parsed: dict[tuple[int, int, int], str] = {}
    for tag in tags:
        version = _version_tuple(tag)
        if version in parsed:
            raise ValueError(f"duplicate semantic version: {tag}")
        parsed[version] = tag
    return parsed[max(parsed)]


def validate_candidate(
    candidate: str,
    releases: Sequence[Mapping[str, Any]],
) -> str:
    _version_tuple(candidate)
    published_tags: list[str] = []
    for release in releases:
        if release.get("draft") is False and isinstance(
            release.get("published_at"),
            str,
        ):
            tag = release.get("tag_name")
            if not isinstance(tag, str):
                raise ValueError("malformed release tag metadata")
            published_tags.append(tag)
    highest = highest_version(published_tags) if published_tags else None
    if candidate not in published_tags:
        raise ValueError(f"candidate is not a published Release: {candidate}")
    if highest != candidate:
        raise ValueError(
            f"latest promotion would move backward: {candidate} < {highest}"
        )
    return candidate


def _published_releases(repo: str) -> list[Mapping[str, Any]]:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/releases?per_page=100",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("GitHub Release query failed")
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub Release query returned invalid JSON") from exc
    if not isinstance(pages, list):
        raise ValueError("GitHub Release query returned invalid pages")
    releases: list[Mapping[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("GitHub Release query returned invalid page")
        for release in page:
            if not isinstance(release, Mapping):
                raise ValueError(
                    "GitHub Release query returned invalid release"
                )
            releases.append(release)
    return releases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
    )
    args = parser.parse_args()
    try:
        if REPOSITORY.fullmatch(args.repo) is None:
            raise ValueError("repository must use OWNER/REPO")
        print(
            validate_candidate(
                args.candidate,
                _published_releases(args.repo),
            )
        )
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print(f"latest release selection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
