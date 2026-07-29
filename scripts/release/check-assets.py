#!/usr/bin/env python3
"""Build and validate deterministic public release assets."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

from versioning import verify_versions


ROOT = Path(__file__).resolve().parents[2]
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BUNDLE_PATHS = (
    ".env.compose.example",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "THIRD_PARTY_LICENSES.json",
    "compose.yaml",
    "deploy/compose",
    "deploy/helm/polardb-agentic-server",
    "deploy/release/README.md",
    "docs/en/deployment",
    "docs/zh-cn/deployment",
    "scripts/deploy/smoke-helm.sh",
)


def asset_names(version: str) -> list[str]:
    if not VERSION.fullmatch(version):
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    base = f"polardb-agentic-server-{version}"
    return sorted(
        {
            f"{base}-chart.tgz",
            f"{base}-deploy.tar.gz",
            f"{base}-image-linux-amd64.tar.gz",
            f"{base}-image-linux-arm64.tar.gz",
            f"{base}.spdx.json",
            "SHA256SUMS",
        }
    )


def _files(paths: tuple[str, ...]) -> list[tuple[Path, str]]:
    result: list[tuple[Path, str]] = []
    for relative in paths:
        source = ROOT / relative
        if source.is_file():
            result.append((source, relative))
        elif source.is_dir():
            result.extend(
                (path, path.relative_to(ROOT).as_posix())
                for path in sorted(source.rglob("*"))
                if path.is_file()
            )
        else:
            raise ValueError(f"missing bundle input: {relative}")
    return sorted(result, key=lambda item: item[1])


def _tar_gz(output: Path, files: list[tuple[Path, str]], prefix: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for source, relative in files:
                    data = source.read_bytes()
                    info = tarfile.TarInfo(f"{prefix}/{relative}")
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mode = 0o755 if os.access(source, os.X_OK) else 0o644
                    info.pax_headers = {}
                    archive.addfile(info, io.BytesIO(data))


def build_bundle(version: str, output_directory: Path) -> None:
    asset_names(version)
    verify_versions(ROOT, version)
    if not output_directory.is_absolute():
        raise ValueError("output directory must be absolute")
    output_directory.mkdir(parents=True, exist_ok=True)
    base = f"polardb-agentic-server-{version}"
    _tar_gz(
        output_directory / f"{base}-deploy.tar.gz",
        _files(BUNDLE_PATHS),
        base,
    )
    chart_root = ROOT / "deploy/helm/polardb-agentic-server"
    chart_files = [
        (path, path.relative_to(chart_root).as_posix())
        for path in sorted(chart_root.rglob("*"))
        if path.is_file()
    ]
    _tar_gz(
        output_directory / f"{base}-chart.tgz",
        chart_files,
        "polardb-agentic-server",
    )


def _safe_archive(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
                raise ValueError(f"unsafe archive member in {path.name}")
            if member.mtime != 0:
                raise ValueError(f"non-deterministic timestamp in {path.name}")


def verify_assets(version: str, directory: Path) -> None:
    expected = set(asset_names(version))
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != expected:
        raise ValueError(f"asset set mismatch: expected {sorted(expected)}, found {sorted(actual)}")
    base = f"polardb-agentic-server-{version}"
    _safe_archive(directory / f"{base}-chart.tgz")
    _safe_archive(directory / f"{base}-deploy.tar.gz")
    checksums: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", maxsplit=1)
        checksums[name] = digest
    for name in sorted(expected - {"SHA256SUMS"}):
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if checksums.get(name) != digest:
            raise ValueError(f"checksum mismatch: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    names_parser = subparsers.add_parser("names")
    names_parser.add_argument("--version", required=True)
    bundle_parser = subparsers.add_parser("build-bundle")
    bundle_parser.add_argument("--version", required=True)
    bundle_parser.add_argument("--output-directory", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "names":
            print(json.dumps(asset_names(args.version)))
        elif args.command == "build-bundle":
            build_bundle(args.version, args.output_directory)
        else:
            verify_assets(args.version, args.directory.resolve())
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"release asset error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
