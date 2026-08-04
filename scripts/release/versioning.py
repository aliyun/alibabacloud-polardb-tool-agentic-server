"""Read, verify, and update release versions in explicit project files."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class TextVersion:
    label: str
    path: str
    pattern: re.Pattern[str]


TEXT_VERSIONS = (
    TextVersion(
        "Canonical Docker deployment skill",
        ".agents/skills/deploy-polardb-agentic-server/scripts/deploy-docker.sh",
        re.compile(
            r'(?m)^PAS_VERSION="\$\{PAS_VERSION:-(\d+\.\d+\.\d+)\}"$'
        ),
    ),
    TextVersion(
        "Canonical source deployment skill",
        ".agents/skills/deploy-polardb-agentic-server/scripts/deploy-source.sh",
        re.compile(
            r'(?m)^PAS_VERSION="\$\{PAS_VERSION:-(\d+\.\d+\.\d+)\}"$'
        ),
    ),
    TextVersion(
        "Claude Docker deployment skill",
        ".claude/skills/deploy-polardb-agentic-server/scripts/deploy-docker.sh",
        re.compile(
            r'(?m)^PAS_VERSION="\$\{PAS_VERSION:-(\d+\.\d+\.\d+)\}"$'
        ),
    ),
    TextVersion(
        "Claude source deployment skill",
        ".claude/skills/deploy-polardb-agentic-server/scripts/deploy-source.sh",
        re.compile(
            r'(?m)^PAS_VERSION="\$\{PAS_VERSION:-(\d+\.\d+\.\d+)\}"$'
        ),
    ),
    TextVersion(
        "English agent-assisted deployment guide",
        "docs/en/deployment/agent-assisted-deployment.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Chinese agent-assisted deployment guide",
        "docs/zh-cn/deployment/agent-assisted-deployment.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Runtime",
        "server/version.py",
        re.compile(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"$'),
    ),
    TextVersion(
        "Python lock",
        "uv.lock",
        re.compile(
            r"(?m)^\[\[package\]\]\n"
            r'name = "alibabacloud-polardb-tool-agentic-server"\n'
            r'version = "(\d+\.\d+\.\d+)"$'
        ),
    ),
    TextVersion(
        "Docker",
        "Dockerfile",
        re.compile(r"(?m)^ARG VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Compose",
        "compose.yaml",
        re.compile(
            r"(?m)^  image: \$\{PAS_IMAGE:-"
            r"ghcr\.io/aliyun/alibabacloud-polardb-tool-agentic-server:"
            r"(\d+\.\d+\.\d+)\}$"
        ),
    ),
    TextVersion(
        "Compose environment example",
        ".env.compose.example",
        re.compile(
            r"(?m)^PAS_IMAGE=ghcr\.io/aliyun/"
            r"alibabacloud-polardb-tool-agentic-server:"
            r"(\d+\.\d+\.\d+)$"
        ),
    ),
    TextVersion(
        "External MySQL Compose",
        "deploy/compose/compose.external-mysql.yaml",
        re.compile(
            r"(?m)^  image: \$\{PAS_IMAGE:-"
            r"ghcr\.io/aliyun/alibabacloud-polardb-tool-agentic-server:"
            r"(\d+\.\d+\.\d+)\}$"
        ),
    ),
    TextVersion(
        "External PostgreSQL Compose",
        "deploy/compose/compose.external-postgres.yaml",
        re.compile(
            r"(?m)^  image: \$\{PAS_IMAGE:-"
            r"ghcr\.io/aliyun/alibabacloud-polardb-tool-agentic-server:"
            r"(\d+\.\d+\.\d+)\}$"
        ),
    ),
    TextVersion(
        "Compose environment generator",
        "scripts/deploy/create-external-mysql-env.sh",
        re.compile(
            r"(?m)^DEFAULT_PAS_IMAGE=ghcr\.io/aliyun/"
            r"alibabacloud-polardb-tool-agentic-server:"
            r"(\d+\.\d+\.\d+)$"
        ),
    ),
    TextVersion(
        "Helm values",
        "deploy/helm/polardb-agentic-server/values.yaml",
        re.compile(r'(?m)^  tag: "(\d+\.\d+\.\d+)"$'),
    ),
    TextVersion(
        "English Compose guide",
        "docs/en/getting-started/deploy-compose.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Chinese Compose guide",
        "docs/zh-cn/getting-started/deploy-compose.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "English prerequisites",
        "docs/en/deployment/prerequisites.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Chinese prerequisites",
        "docs/zh-cn/deployment/prerequisites.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "English Helm install guide",
        "docs/en/deployment/kubernetes-helm.md",
        re.compile(
            r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)\nhelm lint"
        ),
    ),
    TextVersion(
        "English Helm smoke guide",
        "docs/en/deployment/kubernetes-helm.md",
        re.compile(
            r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)\n"
            r"scripts/deploy/smoke-helm\.sh"
        ),
    ),
    TextVersion(
        "Chinese Helm install guide",
        "docs/zh-cn/deployment/kubernetes-helm.md",
        re.compile(
            r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)\nhelm lint"
        ),
    ),
    TextVersion(
        "Chinese Helm smoke guide",
        "docs/zh-cn/deployment/kubernetes-helm.md",
        re.compile(
            r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)\n"
            r"scripts/deploy/smoke-helm\.sh"
        ),
    ),
    TextVersion(
        "English offline installation guide",
        "docs/en/deployment/offline-installation.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Chinese offline installation guide",
        "docs/zh-cn/deployment/offline-installation.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "English upgrade guide",
        "docs/en/deployment/upgrade-and-rollback.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
    TextVersion(
        "Chinese upgrade guide",
        "docs/zh-cn/deployment/upgrade-and-rollback.md",
        re.compile(r"(?m)^PAS_VERSION=(\d+\.\d+\.\d+)$"),
    ),
)


def _one_match(root: Path, location: TextVersion) -> str:
    content = (root / location.path).read_text(encoding="utf-8")
    matches = location.pattern.findall(content)
    if len(matches) != 1:
        raise ValueError(
            f"{location.label} must contain exactly one release version"
        )
    return matches[0]


def _chart_versions(path: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"(version|appVersion):\s*[\"']?([^\"'\s]+)[\"']?",
            line,
        )
        if match:
            values[match.group(1)] = match.group(2)
    if set(values) != {"version", "appVersion"}:
        raise ValueError("Chart.yaml must define version and appVersion")
    return values["version"], values["appVersion"]


def read_versions(root: Path) -> dict[str, str]:
    root = root.resolve()
    python_version = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    web = json.loads((root / "web/package.json").read_text(encoding="utf-8"))
    lock = json.loads(
        (root / "web/package-lock.json").read_text(encoding="utf-8")
    )
    chart_version, app_version = _chart_versions(
        root / "deploy/helm/polardb-agentic-server/Chart.yaml"
    )
    versions = {
        "Python": str(python_version),
        "Web": str(web["version"]),
        "Web lock": str(lock["version"]),
        "Web lock package": str(lock["packages"][""]["version"]),
        "Chart": chart_version,
        "Chart appVersion": app_version,
    }
    versions.update(
        {
            location.label: _one_match(root, location)
            for location in TEXT_VERSIONS
        }
    )
    return versions


def verify_versions(root: Path, expected: str) -> dict[str, str]:
    if not VERSION.fullmatch(expected):
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    versions = read_versions(root)
    mismatches = {
        name: value for name, value in versions.items() if value != expected
    }
    if mismatches:
        rendered = ", ".join(
            f"{name}={value}" for name, value in sorted(mismatches.items())
        )
        raise ValueError(f"expected {expected}: {rendered}")
    return versions


def _replace_one(
    content: str,
    pattern: re.Pattern[str],
    old: str,
    new: str,
    label: str,
) -> str:
    matches = pattern.findall(content)
    if matches != [old]:
        raise ValueError(
            f"{label} does not contain exactly one expected version {old}"
        )
    start, end = next(pattern.finditer(content)).span(1)
    return content[:start] + new + content[end:]


def planned_replacements(
    root: Path,
    old: str,
    new: str,
) -> dict[Path, str]:
    if not VERSION.fullmatch(old) or not VERSION.fullmatch(new):
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    root = root.resolve()
    replacements: dict[Path, str] = {}

    simple_locations = (
        TextVersion(
            "Python",
            "pyproject.toml",
            re.compile(r'(?m)^version = "(\d+\.\d+\.\d+)"$'),
        ),
        TextVersion(
            "Chart",
            "deploy/helm/polardb-agentic-server/Chart.yaml",
            re.compile(r"(?m)^version: (\d+\.\d+\.\d+)$"),
        ),
        TextVersion(
            "Chart appVersion",
            "deploy/helm/polardb-agentic-server/Chart.yaml",
            re.compile(r'(?m)^appVersion: "(\d+\.\d+\.\d+)"$'),
        ),
        *TEXT_VERSIONS,
    )
    for location in simple_locations:
        path = root / location.path
        content = replacements.get(
            path,
            path.read_text(encoding="utf-8"),
        )
        replacements[path] = _replace_one(
            content,
            location.pattern,
            old,
            new,
            location.label,
        )
    package_path = root / "web/package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("version") != old:
        raise ValueError(f"Web does not contain expected version {old}")
    package["version"] = new
    replacements[package_path] = json.dumps(
        package,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    lock_path = root / "web/package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if (
        lock.get("version") != old
        or lock.get("packages", {}).get("", {}).get("version") != old
    ):
        raise ValueError(
            f"Web lock does not contain expected version {old}"
        )
    lock["version"] = new
    lock["packages"][""]["version"] = new
    replacements[lock_path] = json.dumps(
        lock,
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    return replacements
