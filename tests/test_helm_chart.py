from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/polardb-agentic-server"


def _render(*values: str) -> list[dict]:
    if shutil.which("helm") is None:
        pytest.skip("Helm is not installed")
    command = [
        "helm",
        "template",
        "pas",
        str(CHART),
        "--namespace",
        "pas-system",
        "--set",
        "existingSecret=pas-bootstrap",
    ]
    for value in values:
        command.extend(("--set", value))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [
        document
        for document in yaml.safe_load_all(result.stdout)
        if document
    ]


def _kind(documents: list[dict], kind: str) -> dict:
    return next(document for document in documents if document["kind"] == kind)


def test_chart_renders_secure_deployment_and_migration_hook() -> None:
    documents = _render()
    deployment = _kind(documents, "Deployment")
    job = _kind(documents, "Job")
    pdb = _kind(documents, "PodDisruptionBudget")

    assert not any(document["kind"] == "Secret" for document in documents)
    assert deployment["spec"]["replicas"] == 2
    assert deployment["spec"]["strategy"]["rollingUpdate"] == {
        "maxUnavailable": 0,
        "maxSurge": 1,
    }
    assert pdb["spec"]["minAvailable"] == 1
    assert job["metadata"]["annotations"]["helm.sh/hook"] == (
        "pre-install,pre-upgrade"
    )
    assert (
        job["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
        == "before-hook-creation,hook-succeeded"
    )

    app_container = deployment["spec"]["template"]["spec"]["containers"][0]
    migration_container = job["spec"]["template"]["spec"]["containers"][0]
    assert app_container["image"] == migration_container["image"]
    assert migration_container["args"] == ["database", "migrate"]
    assert app_container["args"] == ["serve"]
    for container in (app_container, migration_container):
        assert container["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        assert container["envFrom"] == [
            {"secretRef": {"name": "pas-bootstrap"}}
        ]


def test_chart_mounts_writable_paths_and_exposes_probes() -> None:
    deployment = _kind(_render(), "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == (
        "RuntimeDefault"
    )
    assert {volume["name"] for volume in pod["volumes"]} == {
        "tmp",
        "log",
        "runtime",
    }
    assert {
        mount["mountPath"] for mount in container["volumeMounts"]
    } == {"/tmp", "/app/log", "/var/run/pas"}
    assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert "affinity" in pod


def test_chart_supports_digest_and_single_replica_evaluation() -> None:
    documents = _render(
        "replicaCount=1",
        "image.digest=sha256:deadbeef",
    )
    deployment = _kind(documents, "Deployment")
    job = _kind(documents, "Job")
    expected = (
        "ghcr.io/aliyun/"
        "alibabacloud-polardb-tool-agentic-server@sha256:deadbeef"
    )

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["template"]["spec"]["containers"][0][
        "image"
    ] == expected
    assert job["spec"]["template"]["spec"]["containers"][0][
        "image"
    ] == expected


def test_connection_hook_retains_completed_pod_for_helm_logs() -> None:
    test_pod = _kind(_render(), "Pod")

    assert test_pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    assert (
        test_pod["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
        == "before-hook-creation"
    )


def test_notes_and_docs_cover_safe_bootstrap_and_rendered_migration() -> None:
    notes = (
        CHART / "templates/NOTES.txt"
    ).read_text()
    english = (
        ROOT / "docs/en/deployment/kubernetes-helm.md"
    ).read_text()
    chinese = (
        ROOT / "docs/zh-cn/deployment/kubernetes-helm.md"
    ).read_text()

    assert "kubectl cp" in notes
    assert "/var/run/pas/bootstrap-token" in notes
    assert "kubectl apply" in english
    assert "migration" in english.lower()
    assert "kubectl apply" in chinese
    assert "迁移" in chinese
