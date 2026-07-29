from importlib.metadata import distributions

from packaging.utils import canonicalize_name
from packaging.version import Version


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
        "cryptography": Version("48.0.1"),
        "mcp": Version("1.28.1"),
        "pydantic-settings": Version("2.14.2"),
        "starlette": Version("1.3.1"),
    }
    for dependency, minimum in minimum_versions.items():
        assert versions[dependency] >= minimum
