import json
from datetime import date
from importlib.metadata import distributions
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version
import yaml


ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS_PATH = ROOT / "security" / "dependency-vulnerability-exceptions.yaml"


def _installed_versions() -> dict[str, Version]:
    return {
        canonicalize_name(dist.metadata["Name"]): Version(dist.version)
        for dist in distributions()
        if dist.metadata["Name"]
    }


def test_installed_python_dependencies_meet_the_security_baseline() -> None:
    versions = _installed_versions()

    for removed_dependency in ("python-jose", "ecdsa", "pyasn1", "rsa"):
        assert removed_dependency not in versions

    minimum_versions = {
        "aiohttp": Version("3.14.3"),
        "cryptography": Version("48.0.1"),
        "mcp": Version("1.28.1"),
        "pydantic-settings": Version("2.14.2"),
        "starlette": Version("1.3.1"),
    }
    for dependency, minimum in minimum_versions.items():
        assert versions[dependency] >= minimum


def test_locked_react_router_dependencies_meet_the_security_baseline() -> None:
    lock = json.loads(
        (ROOT / "web" / "package-lock.json").read_text(encoding="utf-8")
    )
    packages = lock["packages"]

    for package_path in (
        "node_modules/react-router",
        "node_modules/react-router-dom",
    ):
        assert Version(packages[package_path]["version"]) >= Version("7.18.0")


def test_dependency_vulnerability_exceptions_are_complete_and_current() -> None:
    policy = yaml.safe_load(EXCEPTIONS_PATH.read_text(encoding="utf-8"))

    assert policy["schema_version"] == 1
    exceptions = policy["exceptions"]
    assert {item["advisory"] for item in exceptions} == {
        "GHSA-g6cj-pr64-35w5",
        "GHSA-qhqw-rrw9-25rm",
        "GHSA-qwww-vcr4-c8h2",
    }

    required_fields = {
        "advisory",
        "package",
        "ecosystem",
        "severity",
        "affected_versions",
        "release",
        "scope",
        "rationale",
        "mitigation",
        "owner",
        "accepted_on",
        "expires_on",
        "advisory_url",
    }
    for item in exceptions:
        assert required_fields <= item.keys()
        assert all(item[field] for field in required_fields)
        assert item["owner"] == "PAS maintainers"
        assert item["release"] == "v0.0.6"
        assert item["expires_on"] == date(2026, 8, 31)
        assert item["expires_on"] >= date.today()
        assert item["advisory_url"] == (
            f"https://github.com/advisories/{item['advisory']}"
        )

    identities = {
        item["advisory"]: (
            item["package"],
            item["ecosystem"],
            item["severity"],
            item["affected_versions"],
        )
        for item in exceptions
    }
    assert identities == {
        "GHSA-g6cj-pr64-35w5": (
            "cryptography",
            "pip",
            "high",
            ">=44.0.0,<50.0.0",
        ),
        "GHSA-qhqw-rrw9-25rm": (
            "asyncmy",
            "pip",
            "critical",
            "<=0.2.11",
        ),
        "GHSA-qwww-vcr4-c8h2": (
            "react-router",
            "npm",
            "high",
            ">=7.12.0,<8.3.0",
        ),
    }

    accepted_on = {
        item["advisory"]: item["accepted_on"] for item in exceptions
    }
    assert accepted_on == {
        "GHSA-g6cj-pr64-35w5": date(2026, 8, 6),
        "GHSA-qhqw-rrw9-25rm": date(2026, 7, 31),
        "GHSA-qwww-vcr4-c8h2": date(2026, 7, 31),
    }
