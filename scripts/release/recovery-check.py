#!/usr/bin/env python3
"""Validate immutable evidence before recovering an incomplete Release."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ValueError(f"cannot load release helper: {path.name}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    try:
        specification.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _platform_resolver():
    return _load_module(
        "recovery_platform_digest",
        SCRIPT_DIR / "resolve-platform-digest.py",
    ).resolve_platform_digest


def _one_version(
    source: Path,
    relative: str,
    pattern: str,
    label: str,
) -> str:
    matches = re.findall(
        pattern,
        (source / relative).read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError(
            f"{label} must contain exactly one release version"
        )
    return matches[0]


def _legacy_source_versions(source: Path) -> dict[str, str]:
    package = json.loads(
        (source / "web/package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (source / "web/package-lock.json").read_text(encoding="utf-8")
    )
    python_lock = tomllib.loads(
        (source / "uv.lock").read_text(encoding="utf-8")
    )
    python_lock_matches = [
        package["version"]
        for package in python_lock.get("package", [])
        if package.get("name")
        == "alibabacloud-polardb-tool-agentic-server"
    ]
    if len(python_lock_matches) != 1:
        raise ValueError(
            "Python lock must contain exactly one release version"
        )
    versions = {
        "Python": str(
            tomllib.loads(
                (source / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["version"]
        ),
        "Web": str(package["version"]),
        "Web lock": str(lock["version"]),
        "Web lock package": str(lock["packages"][""]["version"]),
        "Python lock": str(python_lock_matches[0]),
    }
    locations = (
        (
            "Runtime",
            "server/version.py",
            r'^__version__ = "(\d+\.\d+\.\d+)"$',
        ),
        (
            "Docker",
            "Dockerfile",
            r"^ARG VERSION=(\d+\.\d+\.\d+)$",
        ),
        (
            "Compose",
            "compose.yaml",
            (
                r"^  image: \$\{PAS_IMAGE:-ghcr\.io/aliyun/"
                r"alibabacloud-polardb-tool-agentic-server:"
                r"(\d+\.\d+\.\d+)\}$"
            ),
        ),
        (
            "External MySQL Compose",
            "deploy/compose/compose.external-mysql.yaml",
            (
                r"^  image: \$\{PAS_IMAGE:-ghcr\.io/aliyun/"
                r"alibabacloud-polardb-tool-agentic-server:"
                r"(\d+\.\d+\.\d+)\}$"
            ),
        ),
        (
            "Helm Chart",
            "deploy/helm/polardb-agentic-server/Chart.yaml",
            r"^version: (\d+\.\d+\.\d+)$",
        ),
        (
            "Helm appVersion",
            "deploy/helm/polardb-agentic-server/Chart.yaml",
            r'^appVersion: "(\d+\.\d+\.\d+)"$',
        ),
        (
            "Helm values",
            "deploy/helm/polardb-agentic-server/values.yaml",
            r'^  tag: "(\d+\.\d+\.\d+)"$',
        ),
        (
            "English Compose guide",
            "docs/en/getting-started/deploy-compose.md",
            r"refs/tags/v(\d+\.\d+\.\d+)\.tar\.gz",
        ),
        (
            "Chinese Compose guide",
            "docs/zh-cn/getting-started/deploy-compose.md",
            r"refs/tags/v(\d+\.\d+\.\d+)\.tar\.gz",
        ),
        (
            "English prerequisites",
            "docs/en/deployment/prerequisites.md",
            (
                r"ghcr\.io/aliyun/"
                r"alibabacloud-polardb-tool-agentic-server:"
                r"(\d+\.\d+\.\d+)"
            ),
        ),
        (
            "Chinese prerequisites",
            "docs/zh-cn/deployment/prerequisites.md",
            (
                r"ghcr\.io/aliyun/"
                r"alibabacloud-polardb-tool-agentic-server:"
                r"(\d+\.\d+\.\d+)"
            ),
        ),
    )
    versions.update(
        {
            label: _one_version(source, relative, pattern, label)
            for label, relative, pattern in locations
        }
    )
    return versions


def discover_source_versions(source: Path) -> dict[str, str]:
    module = _load_module(
        "recovery_versioning",
        SCRIPT_DIR / "versioning.py",
    )
    try:
        return module.read_versions(source)
    except (FileNotFoundError, ValueError):
        return _legacy_source_versions(source)


def validate_recovery(
    evidence: Mapping[str, Any],
    *,
    tag: str,
    expected_commit: str,
) -> dict[str, Any]:
    match = TAG.fullmatch(tag)
    if match is None:
        raise ValueError("tag must use vMAJOR.MINOR.PATCH")
    if SHA.fullmatch(expected_commit) is None:
        raise ValueError("expected commit must be a full lowercase SHA")
    version = tag.removeprefix("v")
    if evidence.get("tag_commit") != expected_commit:
        raise ValueError("tag commit mismatch")
    if evidence.get("reachable_from_main") is not True:
        raise ValueError("tag is not reachable from public main")
    versions = evidence.get("source_versions")
    if not isinstance(versions, Mapping) or not versions:
        raise ValueError("tagged source version evidence is missing")
    if any(value != version for value in versions.values()):
        raise ValueError("tagged source version mismatch")
    labels = evidence.get("image_labels")
    if not isinstance(labels, Mapping) or (
        labels.get("org.opencontainers.image.version") != version
        or labels.get("org.opencontainers.image.revision")
        != expected_commit
    ):
        raise ValueError("image label mismatch")
    index_digest = evidence.get("image_index_digest")
    if not isinstance(index_digest, str) or DIGEST.fullmatch(
        index_digest
    ) is None:
        raise ValueError("image index digest is invalid")
    index = evidence.get("image_index")
    if not isinstance(index, Mapping):
        raise ValueError("image index evidence is missing")
    resolve = _platform_resolver()
    platform_digests = {
        "linux/amd64": resolve(index, "linux", "amd64"),
        "linux/arm64": resolve(index, "linux", "arm64"),
    }
    if evidence.get("chart_version") != version:
        raise ValueError("Chart version mismatch")
    if evidence.get("release_exists") is not False:
        raise ValueError("GitHub Release already exists")
    return {
        "tag": tag,
        "commit": expected_commit,
        "image_index_digest": index_digest,
        "platform_digests": platform_digests,
        "chart_version": version,
        "release_exists": False,
    }


def _run(
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise ValueError(f"command failed: {arguments[0]}")
    return result


def _json_command(arguments: list[str]) -> Any:
    result = _run(arguments)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"command returned invalid JSON: {arguments[0]}"
        ) from exc


def _image_labels(image: str) -> Mapping[str, str]:
    image_config = _json_command(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            "{{json .Image}}",
        ]
    )
    if not isinstance(image_config, Mapping):
        raise ValueError("image configuration is invalid")
    config = image_config.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("image configuration is invalid")
    labels = config.get("Labels")
    if labels is None:
        labels = config.get("labels")
    if not isinstance(labels, Mapping):
        raise ValueError("image labels are missing")
    return {str(key): str(value) for key, value in labels.items()}


def _chart_version(output: str) -> str:
    matches = re.findall(r"(?m)^version:\s*[\"']?([^\"'\s]+)", output)
    if len(matches) != 1:
        raise ValueError("Chart version evidence is invalid")
    return matches[0]


def gather_evidence(
    *,
    repo: str,
    tag: str,
    expected_commit: str,
    source: Path,
    image: str,
    chart: str,
) -> dict[str, Any]:
    version = tag.removeprefix("v")
    tag_commit = _run(
        ["gh", "api", f"repos/{repo}/commits/{tag}", "--jq", ".sha"]
    ).stdout.strip()
    comparison = _run(
        [
            "gh",
            "api",
            f"repos/{repo}/compare/{tag}...main",
            "--jq",
            ".status",
        ]
    ).stdout.strip()
    image_reference = f"{image}:{version}"
    index = _json_command(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            image_reference,
        ]
    )
    manifest = _json_command(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image_reference,
            "--format",
            "{{json .Manifest}}",
        ]
    )
    if not isinstance(manifest, Mapping):
        raise ValueError("image manifest evidence is invalid")
    index_digest = manifest.get("digest")
    if index_digest is None:
        index_digest = manifest.get("Digest")
    resolve = _platform_resolver()
    child_digests = (
        resolve(index, "linux", "amd64"),
        resolve(index, "linux", "arm64"),
    )
    platform_labels = [
        _image_labels(f"{image}@{digest}") for digest in child_digests
    ]
    if platform_labels[0] != platform_labels[1]:
        raise ValueError("image labels differ across platforms")
    chart_output = _run(
        [
            "helm",
            "show",
            "chart",
            chart,
            "--version",
            version,
        ]
    ).stdout
    release = _run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--silent"],
        check=False,
    )
    if release.returncode == 0:
        release_exists = True
    elif "404" in release.stderr or "Not Found" in release.stderr:
        release_exists = False
    else:
        raise ValueError("GitHub Release lookup failed")
    return {
        "tag_commit": tag_commit,
        "reachable_from_main": comparison in {"ahead", "identical"},
        "source_versions": discover_source_versions(source),
        "image_index_digest": index_digest,
        "image_labels": platform_labels[0],
        "image_index": index,
        "chart_version": _chart_version(chart_output),
        "release_exists": release_exists,
        "expected_commit": expected_commit,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument(
        "--image",
        default=(
            "ghcr.io/aliyun/"
            "alibabacloud-polardb-tool-agentic-server"
        ),
    )
    parser.add_argument(
        "--chart",
        default=(
            "oci://ghcr.io/aliyun/charts/"
            "polardb-agentic-server"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args()
    try:
        if REPOSITORY.fullmatch(args.repo) is None:
            raise ValueError("repository must use OWNER/REPO")
        if TAG.fullmatch(args.tag) is None:
            raise ValueError("tag must use vMAJOR.MINOR.PATCH")
        if SHA.fullmatch(args.expected_commit) is None:
            raise ValueError("expected commit must be a full lowercase SHA")
        evidence = gather_evidence(
            repo=args.repo,
            tag=args.tag,
            expected_commit=args.expected_commit,
            source=args.source.resolve(),
            image=args.image,
            chart=args.chart,
        )
        summary = validate_recovery(
            evidence,
            tag=args.tag,
            expected_commit=args.expected_commit,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"release recovery check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
