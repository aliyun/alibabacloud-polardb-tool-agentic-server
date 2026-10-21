#!/usr/bin/env python3
"""Validate a public snapshot, directly or inside an unchanged merge wrapper."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


ALLOWED_TYPES = (
    "feat",
    "fix",
    "docs",
    "build",
    "perf",
    "refactor",
    "security",
    "release",
)
TYPE_PATTERN = "|".join(ALLOWED_TYPES)
SUBJECT = re.compile(
    rf"^(?P<type>{TYPE_PATTERN})"
    r"(?:\([a-z0-9._/-]+\))?!?: "
    r"(?P<description>.+)$"
)
EMPTY_DESCRIPTION = re.compile(
    rf"^(?:{TYPE_PATTERN})(?:\([a-z0-9._/-]+\))?!?:\s*$"
)
VERSION = re.compile(
    r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)
SHA = re.compile(r"^[0-9a-f]{40}$")
GENERIC_SUBJECTS = (
    re.compile(r"\bpublish\s+v\d", re.IGNORECASE),
    re.compile(r"\bport\s+develop\b", re.IGNORECASE),
    re.compile(r"\bopen\s+source\s+release\b", re.IGNORECASE),
)


def _trailers(body: str, name: str) -> list[str]:
    prefix = f"{name}:"
    return [
        line.removeprefix(prefix).strip()
        for line in body.splitlines()
        if line.startswith(prefix)
    ]


def validate_message(subject: str, body: str) -> None:
    if subject.lower().startswith("merge "):
        raise ValueError("merge subjects are not allowed on public main")
    if len(subject) > 72:
        raise ValueError("commit subject must not exceed 72 characters")
    match = SUBJECT.fullmatch(subject)
    if match is None:
        if EMPTY_DESCRIPTION.fullmatch(subject):
            raise ValueError(
                "commit subject requires a behavior description"
            )
        raise ValueError(
            "commit subject must use an allowed Conventional Commit type"
        )
    description = match.group("description").strip()
    if VERSION.fullmatch(description) or re.fullmatch(
        r"(?:release\s+)?v\d+\.\d+\.\d+",
        description,
        flags=re.IGNORECASE,
    ):
        raise ValueError(
            "commit subject requires a behavior description, not only a version"
        )
    if any(pattern.search(subject) for pattern in GENERIC_SUBJECTS):
        raise ValueError("generic public release subject is not allowed")

    release_versions = _trailers(body, "Release-Version")
    source_commits = _trailers(body, "Source-Develop")
    if not release_versions:
        raise ValueError("missing Release-Version trailer")
    if len(release_versions) != 1:
        raise ValueError("require exactly one Release-Version trailer")
    if not source_commits:
        raise ValueError("missing Source-Develop trailer")
    if len(source_commits) != 1:
        raise ValueError("require exactly one Source-Develop trailer")
    if VERSION.fullmatch(release_versions[0]) is None:
        raise ValueError(
            "Release-Version trailer must use vMAJOR.MINOR.PATCH"
        )
    if SHA.fullmatch(source_commits[0]) is None:
        raise ValueError(
            "Source-Develop trailer must use a full lowercase SHA"
        )


def _commit_message(commit: str) -> tuple[str, str]:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%s%x00%b", commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("cannot read public commit")
    parts = result.stdout.split("\0", maxsplit=1)
    if len(parts) != 2:
        raise ValueError("public commit message is malformed")
    return parts[0].rstrip("\n"), parts[1]


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ValueError("cannot read public commit graph; fetch full history")
    return result.stdout.strip()


def _parents(commit: str) -> list[str]:
    # Object headers retain parent IDs even at a shallow checkout boundary.
    # `git show --format=%P` can hide those missing parents instead.
    headers = _git("cat-file", "-p", commit).split("\n\n", maxsplit=1)[0]
    return [
        line.removeprefix("parent ")
        for line in headers.splitlines()
        if line.startswith("parent ")
    ]


def validate_commit(commit: str) -> str:
    """Return the checked snapshot SHA without trusting merge-wrapper metadata."""
    commit = _git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
    parents = _parents(commit)
    snapshot = commit
    if len(parents) > 2:
        raise ValueError("a public snapshot merge must have exactly two parents")
    if len(parents) == 2:
        main, snapshot = parents
        if len(_parents(snapshot)) > 1:
            raise ValueError("the snapshot must not itself be a merge")
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", main, snapshot],
            capture_output=True, text=True, check=False,
        )
        if ancestry.returncode == 1:
            raise ValueError(
                "snapshot must include the first-parent main commit; "
                "refresh the snapshot against current main"
            )
        if ancestry.returncode != 0:
            raise ValueError("cannot read public commit graph; fetch full history")
        if _git("rev-parse", f"{commit}^{{tree}}") != _git("rev-parse", f"{snapshot}^{{tree}}"):
            raise ValueError("merge tree differs from the snapshot; prepare and review a new snapshot")

    subject, body = _commit_message(snapshot)
    validate_message(subject, body)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("commit")
    args = parser.parse_args()
    try:
        snapshot = validate_commit(args.commit)
        print(f"public commit message: ok (snapshot {snapshot})")
        return 0
    except (OSError, ValueError) as exc:
        print(f"public commit validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
