from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
RELEASE_BASE = f"polardb-agentic-server-{CURRENT_VERSION}"
EXPECTED_ASSETS = {
    f"{RELEASE_BASE}-chart.tgz",
    f"{RELEASE_BASE}-deploy.tar.gz",
    f"{RELEASE_BASE}-image-linux-amd64.tar.gz",
    f"{RELEASE_BASE}-image-linux-arm64.tar.gz",
    f"{RELEASE_BASE}.spdx.json",
    "SHA256SUMS",
}
AMD64_DIGEST = "sha256:" + "a" * 64
ARM64_DIGEST = "sha256:" + "b" * 64
INDEX_DIGEST = "sha256:" + "c" * 64
OCI_INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": AMD64_DIGEST,
            "size": 123,
            "platform": {"architecture": "amd64", "os": "linux"},
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": ARM64_DIGEST,
            "size": 124,
            "platform": {"architecture": "arm64", "os": "linux"},
        },
    ],
}


def _load_platform_resolver():
    path = ROOT / "scripts/release/resolve-platform-digest.py"
    specification = importlib.util.spec_from_file_location(
        "resolve_platform_digest",
        path,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.resolve_platform_digest


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_version_matches_all_versioned_components() -> None:
    result = _run(
        "scripts/release/verify-version.py",
        f"v{CURRENT_VERSION}",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == CURRENT_VERSION


def test_release_version_rejects_invalid_or_mismatched_tag() -> None:
    invalid = _run("scripts/release/verify-version.py", CURRENT_VERSION)
    mismatch = _run("scripts/release/verify-version.py", "v9.9.9")

    assert invalid.returncode != 0
    assert mismatch.returncode != 0


def test_expected_release_asset_names_are_exact() -> None:
    result = _run(
        "scripts/release/check-assets.py",
        "names",
        "--version",
        CURRENT_VERSION,
    )

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == EXPECTED_ASSETS


def test_deployment_bundle_is_deterministic_and_allowlisted(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for output in (first, second):
        result = subprocess.run(
            [
                "scripts/release/build-deployment-bundle.sh",
                CURRENT_VERSION,
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    for name in (
        f"{RELEASE_BASE}-chart.tgz",
        f"{RELEASE_BASE}-deploy.tar.gz",
    ):
        assert hashlib.sha256((first / name).read_bytes()).digest() == hashlib.sha256(
            (second / name).read_bytes()
        ).digest()

    bundle = first / f"{RELEASE_BASE}-deploy.tar.gz"
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
    assert members
    assert all(not member.name.startswith("/") and ".." not in Path(member.name).parts for member in members)
    assert all(member.mtime == 0 for member in members)
    assert {Path(member.name).parts[0] for member in members} == {
        RELEASE_BASE
    }
    assert not any("secret" in member.name.lower() for member in members)


def test_deployment_bundle_rejects_version_drift(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "scripts/release/build-deployment-bundle.sh",
            "9.9.9",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "expected 9.9.9" in result.stderr


def test_image_archive_builder_rejects_mutable_reference(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "scripts/release/save-image-archive.sh",
            "--image",
            "ghcr.io/aliyun/example:0.0.1",
            "--platform",
            "linux/amd64",
            "--version",
            "0.0.1",
            "--revision",
            "abc123",
            "--output",
            str(tmp_path / "image.tar.gz"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "digest" in result.stderr.lower()


def test_platform_digest_resolution() -> None:
    resolve = _load_platform_resolver()

    assert resolve(OCI_INDEX, "linux", "amd64") == AMD64_DIGEST
    assert resolve(OCI_INDEX, "linux", "arm64") == ARM64_DIGEST


@pytest.mark.parametrize(
    ("manifest", "platform", "message"),
    [
        (OCI_INDEX, ("linux", "s390x"), "missing"),
        (
            {
                **OCI_INDEX,
                "manifests": [
                    OCI_INDEX["manifests"][0],
                    OCI_INDEX["manifests"][0],
                ],
            },
            ("linux", "amd64"),
            "multiple",
        ),
        (
            {
                **OCI_INDEX,
                "manifests": [
                    {
                        **OCI_INDEX["manifests"][0],
                        "digest": "sha256:not-a-digest",
                    }
                ],
            },
            ("linux", "amd64"),
            "digest",
        ),
        (
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "d" * 64},
                "layers": [],
            },
            ("linux", "amd64"),
            "index",
        ),
    ],
)
def test_platform_digest_resolution_rejects_invalid_indexes(
    manifest: dict[str, object],
    platform: tuple[str, str],
    message: str,
) -> None:
    resolve = _load_platform_resolver()

    with pytest.raises(ValueError, match=message):
        resolve(manifest, *platform)


def test_image_archives_pull_distinct_platform_digests(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    manifest = tmp_path / "index.json"
    manifest.write_text(json.dumps(OCI_INDEX), encoding="utf-8")
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
case "$1 $2 $3" in
  "buildx imagetools inspect")
    cat "$FAKE_OCI_INDEX"
    ;;
  "image inspect --format")
    case "$4" in
      *version*) printf '%s\n' "$EXPECTED_VERSION" ;;
      *revision*) printf '%s\n' "$EXPECTED_REVISION" ;;
      *) exit 91 ;;
    esac
    ;;
  *)
    if [ "$1" = save ]; then
      printf 'archive:%s\n' "$4" >"$3"
    fi
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_OCI_INDEX": str(manifest),
        "EXPECTED_VERSION": CURRENT_VERSION,
        "EXPECTED_REVISION": "abc123",
    }
    image = f"repository@{INDEX_DIGEST}"

    for platform in ("linux/amd64", "linux/arm64"):
        output = tmp_path / f"{platform.replace('/', '-')}.tar.gz"
        result = subprocess.run(
            [
                "scripts/release/save-image-archive.sh",
                "--image",
                image,
                "--platform",
                platform,
                "--version",
                CURRENT_VERSION,
                "--revision",
                "abc123",
                "--output",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert result.returncode == 0, result.stderr

    operations = log.read_text(encoding="utf-8").splitlines()
    pulls = [
        line.removeprefix("pull --platform ").split(" ", maxsplit=1)[1]
        for line in operations
        if line.startswith("pull --platform ")
    ]
    assert pulls == [
        f"repository@{AMD64_DIGEST}",
        f"repository@{ARM64_DIGEST}",
    ]
    assert image not in pulls
