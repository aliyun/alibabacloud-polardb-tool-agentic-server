from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "f60b33cf5fc6a22da5bc0b10e2d42faa74660dae"
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


def _load_recovery_checker():
    path = ROOT / "scripts/release/recovery-check.py"
    specification = importlib.util.spec_from_file_location(
        "recovery_check",
        path,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _evidence() -> dict[str, object]:
    return {
        "tag_commit": COMMIT,
        "reachable_from_main": True,
        "source_versions": {
            "Python": "0.0.2",
            "Web": "0.0.2",
            "Chart": "0.0.2",
        },
        "image_index_digest": INDEX_DIGEST,
        "image_labels": {
            "org.opencontainers.image.version": "0.0.2",
            "org.opencontainers.image.revision": COMMIT,
        },
        "image_index": copy.deepcopy(OCI_INDEX),
        "chart_version": "0.0.2",
        "release_exists": False,
    }


def test_recovery_guards_return_sanitized_summary() -> None:
    checker = _load_recovery_checker()

    summary = checker.validate_recovery(
        _evidence(),
        tag="v0.0.2",
        expected_commit=COMMIT,
    )

    assert summary == {
        "tag": "v0.0.2",
        "commit": COMMIT,
        "image_index_digest": INDEX_DIGEST,
        "platform_digests": {
            "linux/amd64": AMD64_DIGEST,
            "linux/arm64": ARM64_DIGEST,
        },
        "chart_version": "0.0.2",
        "release_exists": False,
    }
    assert "token" not in repr(summary).lower()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"tag_commit": "1" * 40}, "tag commit mismatch"),
        ({"reachable_from_main": False}, "not reachable from public main"),
        (
            {"source_versions": {"Python": "0.0.1"}},
            "tagged source version mismatch",
        ),
        (
            {
                "image_labels": {
                    "org.opencontainers.image.version": "0.0.1",
                    "org.opencontainers.image.revision": COMMIT,
                }
            },
            "image label mismatch",
        ),
        ({"chart_version": "0.0.1"}, "Chart version mismatch"),
        ({"release_exists": True}, "GitHub Release already exists"),
    ],
)
def test_recovery_guards_fail_closed(
    mutation: dict[str, object],
    message: str,
) -> None:
    checker = _load_recovery_checker()
    evidence = _evidence()
    evidence.update(mutation)

    with pytest.raises(ValueError, match=message):
        checker.validate_recovery(
            evidence,
            tag="v0.0.2",
            expected_commit=COMMIT,
        )


@pytest.mark.parametrize(
    ("manifests", "message"),
    [
        ([OCI_INDEX["manifests"][0]], "missing platform descriptor"),
        (
            [
                OCI_INDEX["manifests"][0],
                OCI_INDEX["manifests"][1],
                OCI_INDEX["manifests"][1],
            ],
            "multiple platform descriptors",
        ),
    ],
)
def test_recovery_guards_require_both_platforms_once(
    manifests: list[dict[str, object]],
    message: str,
) -> None:
    checker = _load_recovery_checker()
    evidence = _evidence()
    evidence["image_index"] = {
        **OCI_INDEX,
        "manifests": manifests,
    }

    with pytest.raises(ValueError, match=message):
        checker.validate_recovery(
            evidence,
            tag="v0.0.2",
            expected_commit=COMMIT,
        )


def test_recovery_checker_reads_legacy_tag_version_locations(
    tmp_path: Path,
) -> None:
    checker = _load_recovery_checker()
    source = tmp_path / "tagged"
    files = {
        "pyproject.toml": '[project]\nversion = "0.0.2"\n',
        "server/version.py": '__version__ = "0.0.2"\n',
        "uv.lock": (
            "version = 1\n"
            'revision = 3\n\n'
            "[[package]]\n"
            'name = "alibabacloud-polardb-tool-agentic-server"\n'
            'version = "0.0.2"\n'
        ),
        "Dockerfile": "ARG VERSION=0.0.2\n",
        "compose.yaml": (
            "  image: ${PAS_IMAGE:-ghcr.io/aliyun/"
            "alibabacloud-polardb-tool-agentic-server:0.0.2}\n"
        ),
        "deploy/compose/compose.external-mysql.yaml": (
            "  image: ${PAS_IMAGE:-ghcr.io/aliyun/"
            "alibabacloud-polardb-tool-agentic-server:0.0.2}\n"
        ),
        "deploy/helm/polardb-agentic-server/Chart.yaml": (
            'version: 0.0.2\nappVersion: "0.0.2"\n'
        ),
        "deploy/helm/polardb-agentic-server/values.yaml": (
            '  tag: "0.0.2"\n'
        ),
        "docs/en/getting-started/deploy-compose.md": (
            "refs/tags/v0.0.2.tar.gz\n"
        ),
        "docs/zh-cn/getting-started/deploy-compose.md": (
            "refs/tags/v0.0.2.tar.gz\n"
        ),
        "docs/en/deployment/prerequisites.md": (
            "ghcr.io/aliyun/"
            "alibabacloud-polardb-tool-agentic-server:0.0.2\n"
        ),
        "docs/zh-cn/deployment/prerequisites.md": (
            "ghcr.io/aliyun/"
            "alibabacloud-polardb-tool-agentic-server:0.0.2\n"
        ),
    }
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    package = {"name": "fixture", "version": "0.0.2"}
    lock = {
        **package,
        "lockfileVersion": 3,
        "packages": {"": package},
    }
    for relative, content in (
        ("web/package.json", package),
        ("web/package-lock.json", lock),
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")

    versions = checker.discover_source_versions(source)

    assert versions
    assert set(versions.values()) == {"0.0.2"}
    assert versions["Python lock"] == "0.0.2"
