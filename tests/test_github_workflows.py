from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


class _WorkflowLoader(yaml.SafeLoader):
    pass


_WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first_char, resolvers in list(_WorkflowLoader.yaml_implicit_resolvers.items()):
    _WorkflowLoader.yaml_implicit_resolvers[first_char] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]


def _workflow(name: str) -> dict:
    with (ROOT / f".github/workflows/{name}").open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=_WorkflowLoader)


def _steps(workflow: dict) -> list[dict]:
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
    ]


def test_ci_is_read_only_pinned_and_cancels_stale_runs() -> None:
    workflow = _workflow("ci.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert all(
        FULL_SHA.fullmatch(step["uses"])
        for step in _steps(workflow)
        if "uses" in step
    )
    assert "secrets." not in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_ci_has_all_release_gate_jobs() -> None:
    workflow = _workflow("ci.yml")

    assert {
        "backend",
        "web",
        "migrations",
        "container",
        "helm",
        "public-boundary",
    } <= set(workflow["jobs"])
    assert "services" in workflow["jobs"]["migrations"]
    assert all(
        "services" not in job
        for name, job in workflow["jobs"].items()
        if name != "migrations"
    )


def test_public_commit_policy_validates_only_the_pushed_main_head() -> None:
    workflow = _workflow("ci.yml")
    steps = workflow["jobs"]["public-boundary"]["steps"]
    validators = [
        step
        for step in steps
        if "validate-public-commit.py" in step.get("run", "")
    ]

    assert len(validators) == 1
    assert validators[0]["if"] == (
        "${{ github.event_name == 'push' "
        "&& github.ref == 'refs/heads/main' }}"
    )
    assert validators[0]["run"] == (
        'python scripts/release/validate-public-commit.py "$GITHUB_SHA"'
    )


def test_all_current_workflow_actions_are_pinned() -> None:
    for workflow_path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = _workflow(workflow_path.name)
        for step in _steps(workflow):
            if "uses" in step:
                assert FULL_SHA.fullmatch(step["uses"]), (
                    f"{workflow_path.name}: action is not pinned: {step['uses']}"
                )


def test_release_is_tag_gated_protected_and_minimally_privileged() -> None:
    workflow = _workflow("release.yml")
    release_job = workflow["jobs"]["release"]

    assert workflow["on"]["push"]["tags"] == ["v*.*.*"]
    assert workflow["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert release_job["environment"] == "release"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"


def test_release_builds_and_verifies_immutable_public_artifacts() -> None:
    content = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "git merge-base --is-ancestor" in content
    assert "scripts/release/verify-version.py" in content
    assert "linux/amd64,linux/arm64" in content
    assert "ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server" in content
    assert ":latest" not in content
    assert "docker logout ghcr.io" in content
    assert "docker buildx imagetools inspect" in content
    assert "helm push" in content
    assert "helm show chart" in content
    assert "spdx-json" in content
    assert "actions/attest-build-provenance@" in content
    assert "--draft" in content
    assert "--prerelease" in content


def test_recovery_workflow_is_manual_guarded_and_non_rebuilding() -> None:
    workflow = _workflow("recover-release.yml")
    content = (
        ROOT / ".github/workflows/recover-release.yml"
    ).read_text(encoding="utf-8")

    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["tag"]["required"] == "true"
    assert inputs["expected_commit"]["required"] == "true"
    assert inputs["dry_run"] == {
        "description": "Validate only; do not create a GitHub Release",
        "required": "true",
        "type": "boolean",
        "default": "true",
    }
    validate = workflow["jobs"]["validate"]
    recover = workflow["jobs"]["recover"]
    assert "environment" not in validate
    assert recover["environment"] == "release"
    assert recover["if"] == "${{ inputs.dry_run == false }}"
    assert recover["needs"] == "validate"
    assert "path: tools" in content
    assert "path: tagged" in content
    assert "ref: ${{ inputs.tag }}" in content
    assert "docker build" not in content
    assert "docker push" not in content
    assert "helm push" not in content
    assert "--verify-tag" in content
    assert "--draft" in content
    assert "--prerelease" in content
    assert all(
        FULL_SHA.fullmatch(step["uses"])
        for step in _steps(workflow)
        if "uses" in step
    )


def test_latest_promotion_runs_only_for_published_releases() -> None:
    workflow = _workflow("promote-latest.yml")
    job = workflow["jobs"]["promote"]
    content = (
        ROOT / ".github/workflows/promote-latest.yml"
    ).read_text(encoding="utf-8")

    assert workflow["on"] == {"release": {"types": ["published"]}}
    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert job["environment"] == "release"
    assert "scripts/release/select-latest-release.py" in content
    assert "org.opencontainers.image.version" in content
    assert "org.opencontainers.image.revision" in content
    assert (
        "docker buildx imagetools create --tag \"$IMAGE:latest\""
        in content
    )
    assert "docker logout ghcr.io" in content
    assert "FINAL_DIGEST" in content
    assert "docker/build-push-action@" not in content
    assert "helm push" not in content
    assert all(
        FULL_SHA.fullmatch(step["uses"])
        for step in _steps(workflow)
        if "uses" in step
    )
