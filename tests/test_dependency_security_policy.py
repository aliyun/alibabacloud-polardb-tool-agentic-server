import json
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
        "alibabacloud-credentials": Version("1.0.8"),
        "alibabacloud-tea-openapi": Version("0.4.6"),
        "asyncmy": Version("0.2.12"),
        "cryptography": Version("50.0.1"),
        "mcp": Version("1.28.1"),
        "pydantic-settings": Version("2.14.2"),
        "sqlparse": Version("0.6.0"),
        "starlette": Version("1.3.1"),
    }
    for dependency, minimum in minimum_versions.items():
        assert versions[dependency] >= minimum


def test_locked_web_dependencies_meet_the_security_baseline() -> None:
    lock = json.loads(
        (ROOT / "web" / "package-lock.json").read_text(encoding="utf-8")
    )
    packages = lock["packages"]

    minimum_versions = {
        "node_modules/@typescript-eslint/typescript-estree/node_modules/brace-expansion": Version(
            "5.0.9"
        ),
        "node_modules/brace-expansion": Version("1.1.18"),
        "node_modules/js-yaml": Version("4.3.1"),
        "node_modules/nanoid": Version("3.3.18"),
        "node_modules/react-router": Version("7.18.2"),
        "node_modules/react-router-dom": Version("7.18.2"),
    }
    for package_path, minimum in minimum_versions.items():
        assert Version(packages[package_path]["version"]) >= minimum


def test_dependency_vulnerability_exceptions_are_complete_and_current() -> None:
    policy = yaml.safe_load(EXCEPTIONS_PATH.read_text(encoding="utf-8"))

    assert policy["schema_version"] == 1
    exceptions = policy["exceptions"]
    assert exceptions == []
