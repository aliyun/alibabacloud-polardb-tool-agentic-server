#!/usr/bin/env python3
"""Generate a deterministic license inventory from the committed lockfiles."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "THIRD_PARTY_LICENSES.json"
ALLOWLIST_PATH = ROOT / ".github/allowed-licenses.txt"


def _license_map(groups: dict[str, set[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for license_expression, packages in groups.items():
        for package in packages:
            if package in result:
                raise RuntimeError(f"duplicate Python license entry: {package}")
            result[package] = license_expression
    return result


# Python package metadata is not present in uv.lock. This reviewed SPDX mapping
# is deliberately version-controlled: adding a locked package fails closed
# until its license is reviewed here.
PYTHON_LICENSES = _license_map(
    {
        "Apache-2.0": {
            "aiofiles",
            "aiosignal",
            "alibabacloud-credentials",
            "alibabacloud-credentials-api",
            "alibabacloud-gateway-spi",
            "alibabacloud-polardb20170801",
            "alibabacloud-sts20150401",
            "alibabacloud-tea",
            "alibabacloud-tea-openapi",
            "alibabacloud-tea-util",
            "asyncmy",
            "asyncpg",
            "darabonba-core",
            "frozenlist",
            "multidict",
            "propcache",
            "pytest-asyncio",
            "python-multipart",
            "requests",
            "types-pyyaml",
            "tzdata",
            "websocket-client",
            "yarl",
        },
        "Apache-2.0 AND MIT": {"aiohttp"},
        "Apache-2.0 OR BSD-2-Clause": {"packaging"},
        "Apache-2.0 OR BSD-3-Clause": {"cryptography"},
        "BSD-2-Clause": {"pygments"},
        "BSD-3-Clause": {
            "authlib",
            "click",
            "colorama",
            "httpcore",
            "httpx",
            "idna",
            "joserfc",
            "markupsafe",
            "pycparser",
            "python-dotenv",
            "sqlparse",
            "sse-starlette",
            "starlette",
            "uvicorn",
            "websockets",
        },
        "MIT": {
            "aiosqlite",
            "alembic",
            "annotated-doc",
            "annotated-types",
            "anyio",
            "apscheduler",
            "ast-serialize",
            "attrs",
            "cffi",
            "charset-normalizer",
            "fastapi",
            "h11",
            "httptools",
            "httpx-sse",
            "iniconfig",
            "jsonschema",
            "jsonschema-specifications",
            "librt",
            "mako",
            "mcp",
            "mypy",
            "mypy-extensions",
            "pluggy",
            "pydantic",
            "pydantic-core",
            "pydantic-settings",
            "pyjwt",
            "pytest",
            "pyyaml",
            "referencing",
            "rpds-py",
            "ruff",
            "sqlalchemy",
            "typing-inspection",
            "tzlocal",
            "urllib3",
            "uvloop",
            "watchfiles",
        },
        "MIT AND PSF-2.0": {"greenlet"},
        "MPL-2.0": {"certifi", "pathspec"},
        "PSF-2.0": {"aiohappyeyeballs", "pywin32", "typing-extensions"},
    }
)


def _allowed_licenses() -> set[str]:
    return {
        line.strip()
        for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _python_dependencies() -> list[dict[str, str]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    dependencies: list[dict[str, str]] = []
    locked_names: set[str] = set()
    for package in lock["package"]:
        name = package["name"]
        if name == "alibabacloud-polardb-tool-agentic-server":
            continue
        locked_names.add(name)
        license_expression = PYTHON_LICENSES.get(name)
        if not license_expression:
            raise ValueError(f"unknown Python dependency license: {name}")
        dependencies.append(
            {
                "ecosystem": "python",
                "name": name,
                "version": package["version"],
                "license": license_expression,
            }
        )
    stale = sorted(set(PYTHON_LICENSES) - locked_names)
    if stale:
        raise ValueError(f"stale Python dependency license entries: {', '.join(stale)}")
    return dependencies


def _npm_dependencies() -> list[dict[str, str]]:
    lock = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
    dependencies: list[dict[str, str]] = []
    for package_path, package in lock["packages"].items():
        if not package_path or "version" not in package:
            continue
        license_expression = package.get("license")
        if not license_expression or license_expression in {"UNKNOWN", "UNLICENSED"}:
            raise ValueError(f"unknown npm dependency license: {package_path}")
        dependencies.append(
            {
                "ecosystem": "npm",
                "name": package_path.rsplit("node_modules/", maxsplit=1)[-1],
                "version": package["version"],
                "license": license_expression,
            }
        )
    return dependencies


def build_report() -> dict:
    dependencies = _python_dependencies() + _npm_dependencies()
    dependencies.sort(key=lambda dependency: (dependency["ecosystem"], dependency["name"].lower(), dependency["version"]))
    allowed = _allowed_licenses()
    rejected = sorted(
        {
            dependency["license"]
            for dependency in dependencies
            if dependency["license"] not in allowed
        }
    )
    if rejected:
        raise ValueError(f"disallowed dependency licenses: {', '.join(rejected)}")
    return {
        "schema_version": 1,
        "generated_from": ["uv.lock", "web/package-lock.json"],
        "dependencies": dependencies,
    }


def _serialized_report() -> str:
    return json.dumps(build_report(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the committed report is stale")
    args = parser.parse_args()
    try:
        rendered = _serialized_report()
    except (KeyError, TypeError, ValueError) as exc:
        print(f"license report error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        if not REPORT_PATH.exists() or REPORT_PATH.read_text(encoding="utf-8") != rendered:
            print("license report is stale; run scripts/security/generate-license-report.py", file=sys.stderr)
            return 1
        return 0

    REPORT_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
