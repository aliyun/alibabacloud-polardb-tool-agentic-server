#!/usr/bin/env python3
"""Verify that a release tag matches every versioned project component."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


SEMVER_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _chart_versions(path: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"(version|appVersion):\s*[\"']?([^\"'\s]+)[\"']?", line)
        if match:
            values[match.group(1)] = match.group(2)
    if set(values) != {"version", "appVersion"}:
        raise ValueError("Chart.yaml must define version and appVersion")
    return values["version"], values["appVersion"]


def verify(tag: str, root: Path) -> str:
    match = SEMVER_TAG.fullmatch(tag)
    if not match:
        raise ValueError("release tag must use vMAJOR.MINOR.PATCH")
    version = tag.removeprefix("v")
    python_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    web_version = json.loads((root / "web/package.json").read_text(encoding="utf-8"))["version"]
    lock_version = json.loads((root / "web/package-lock.json").read_text(encoding="utf-8"))["version"]
    chart_version, app_version = _chart_versions(root / "deploy/helm/polardb-agentic-server/Chart.yaml")
    versions = {
        "Python": python_version,
        "Web": web_version,
        "Web lock": lock_version,
        "Chart": chart_version,
        "Chart appVersion": app_version,
    }
    mismatches = {name: value for name, value in versions.items() if value != version}
    if mismatches:
        rendered = ", ".join(f"{name}={value}" for name, value in sorted(mismatches.items()))
        raise ValueError(f"tag {tag} does not match: {rendered}")
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
