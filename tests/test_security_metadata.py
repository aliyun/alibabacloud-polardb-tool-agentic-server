from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(path: str) -> dict:
    with (ROOT / path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_security_policy_requires_private_reports_without_secrets() -> None:
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8").lower()

    assert "v0.0.x" in policy
    assert "pre-release" in policy
    assert "private vulnerability reporting" in policy
    assert "/security/advisories/new" in policy
    assert "do not" in policy and "public issue" in policy
    assert "password" in policy and "token" in policy


def test_dependabot_covers_all_dependency_ecosystems_weekly() -> None:
    config = _load_yaml(".github/dependabot.yml")
    updates = config["updates"]

    assert {update["package-ecosystem"] for update in updates} == {
        "pip",
        "npm",
        "github-actions",
        "docker",
    }
    assert all(update["schedule"]["interval"] == "weekly" for update in updates)
    assert all(update.get("groups") for update in updates)


def test_dependabot_keeps_major_runtime_migrations_separate() -> None:
    config = _load_yaml(".github/dependabot.yml")
    updates = {
        update["package-ecosystem"]: update
        for update in config["updates"]
    }

    npm_group = updates["npm"]["groups"]["web-dependencies"]
    assert npm_group["update-types"] == ["minor", "patch"]

    docker_ignores = {
        entry["dependency-name"]: entry["update-types"]
        for entry in updates["docker"]["ignore"]
    }
    assert docker_ignores == {
        "node": ["version-update:semver-major"],
        "python": ["version-update:semver-major"],
    }


def test_codeql_uses_the_organization_managed_default_setup() -> None:
    assert not (ROOT / ".github/workflows/codeql.yml").exists()


def test_issue_templates_warn_against_secret_disclosure() -> None:
    templates = [
        ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml",
        ROOT / ".github/ISSUE_TEMPLATE/config.yml",
    ]

    for template in templates:
        content = template.read_text(encoding="utf-8").lower()
        assert "secret" in content
        assert "password" in content
        assert "token" in content
