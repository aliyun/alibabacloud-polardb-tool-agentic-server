#!/usr/bin/env python3
"""Allowlist export and redacted defense-in-depth scanning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MANIFEST = ".public-release-manifest.sha256"
INTERNAL_PATH_PARTS = {
    ("docs", "super" + "powers"),
    ("docs", "customer"),
    (".worktrees",),
    (".impeccable",),
}
FORBIDDEN_FILENAMES = {"AGENTS.md", "PRODUCT.md"}
GENERATED_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "dist",
    "data",
    "log",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int | None = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{self.rule}: {location}"


CONTENT_RULES = {
    "INTERNAL_PRIVATE_DOMAIN": re.compile(
        r"(?:gitlab|registry)\." + re.escape("alibaba" + "-inc") + r"\.com",
        re.IGNORECASE,
    ),
    "SECRET_PRIVATE_KEY": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "SECRET_ALIBABA_ACCESS_KEY": re.compile(r"\bLTAI[0-9A-Za-z]{12,}\b"),
    "SECRET_GITHUB_TOKEN": re.compile(r"\b(?:ghp|github_pat)_[0-9A-Za-z_]{20,}\b"),
    "SECRET_BEARER_TOKEN": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b", re.IGNORECASE),
    "CUSTOMER_INSTANCE_ID": re.compile(r"\b(?:pc|rm)-[a-z0-9]{18,}\b", re.IGNORECASE),
}


def _git_files(source: Path, pathspecs: list[str]) -> list[str]:
    command = [
        "git",
        "-C",
        str(source),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *pathspecs,
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        raise ValueError("source must be a Git worktree")
    return sorted({item.decode() for item in result.stdout.split(b"\0") if item})


def _pathspecs(source: Path) -> list[str]:
    allowlist = source / ".public-release-allowlist"
    if not allowlist.is_file():
        raise ValueError("missing .public-release-allowlist")
    result: list[str] = []
    for raw_line in allowlist.read_text(encoding="utf-8").splitlines():
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
            raise ValueError(f"unsafe allowlist pathspec: {value}")
        result.append(value)
    if not result:
        raise ValueError("public release allowlist is empty")
    return result


def _is_script(relative: str, source_file: Path) -> bool:
    path = PurePosixPath(relative)
    if "scripts" in path.parts:
        return True
    try:
        return source_file.read_bytes()[:2] == b"#!"
    except OSError:
        return False


def _scan_path(relative: str) -> list[Finding]:
    path = PurePosixPath(relative)
    findings: list[Finding] = []
    if path.name in FORBIDDEN_FILENAMES:
        findings.append(Finding("INTERNAL_FILENAME", relative))
    if any(tuple(path.parts[: len(parts)]) == parts for parts in INTERNAL_PATH_PARTS):
        findings.append(Finding("INTERNAL_PATH", relative))
    if any(part in GENERATED_PARTS for part in path.parts):
        findings.append(Finding("GENERATED_PATH", relative))
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pyc"}:
        findings.append(Finding("GENERATED_FILE", relative))
    return findings


def _scan_content(path: Path, relative: str) -> list[Finding]:
    content = path.read_bytes()
    if b"\0" in content:
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in CONTENT_RULES.items():
            if pattern.search(line):
                findings.append(Finding(rule, relative, line_number))
    return findings


def _scan_licenses(root: Path) -> list[Finding]:
    report_path = root / "THIRD_PARTY_LICENSES.json"
    allowed_path = root / ".github/allowed-licenses.txt"
    if not report_path.exists() and not allowed_path.exists():
        return []
    if not report_path.exists() or not allowed_path.exists():
        return [Finding("LICENSE_METADATA_INCOMPLETE", str(report_path.relative_to(root)))]
    allowed = {
        line.strip()
        for line in allowed_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    report = json.loads(report_path.read_text(encoding="utf-8"))
    findings = []
    for dependency in report.get("dependencies", []):
        if dependency.get("license") not in allowed:
            findings.append(Finding("LICENSE_NOT_ALLOWED", "THIRD_PARTY_LICENSES.json"))
            break
    return findings


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if ".git" in PurePosixPath(relative).parts:
            continue
        if relative == MANIFEST:
            continue
        findings.extend(_scan_path(relative))
        findings.extend(_scan_content(path, relative))
    findings.extend(_scan_licenses(root))
    return findings


def _write_manifest(root: Path) -> None:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST:
            continue
        entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    content = "".join(f"{digest}  {relative}\n" for relative, digest in entries)
    (root / MANIFEST).write_text(content, encoding="utf-8")
    os.chmod(root / MANIFEST, 0o644)


def export_tree(source: Path, output: Path) -> None:
    source = source.resolve()
    if not output.is_absolute():
        raise ValueError("--output must be an absolute path")
    if output.exists():
        raise ValueError("--output must not already exist")
    files = _git_files(source, _pathspecs(source))
    if not files:
        raise ValueError("allowlist selected no files")
    output.mkdir(mode=0o755, parents=True)
    for relative in files:
        source_file = source / relative
        resolved = source_file.resolve()
        if not resolved.is_relative_to(source):
            raise ValueError(f"symlink escapes source: {relative}")
        if not resolved.is_file():
            raise ValueError(f"allowlisted path is not a regular file: {relative}")
        destination = output / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(resolved, destination)
        os.chmod(destination, 0o755 if _is_script(relative, resolved) else 0o644)
    findings = scan_tree(output)
    if findings:
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        raise ValueError(f"public export rejected with {len(findings)} finding(s)")
    _write_manifest(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--source", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            export_tree(args.source, args.output)
        else:
            findings = scan_tree(args.root.resolve())
            for finding in findings:
                print(finding.render(), file=sys.stderr)
            if findings:
                return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"public release error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
