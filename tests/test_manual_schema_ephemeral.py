from copy import deepcopy

import pytest

from scripts.dev.manual_schema_ephemeral import build_update


@pytest.fixture
def pod():
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "a-0", "namespace": "test",
        "uid": "a-uid", "resourceVersion": "19"}, "spec": {
        "containers": [{"name": "mcp-server", "image": "example/pas:108"}],
        "initContainers": [{"name": "restore-ipflt", "image": "whiteip"},
            {"name": "db-migrate", "image": "example/pas:108", "workingDir": "/app",
             "command": ["/bin/sh", "-c"], "args": ["pas database migrate && exec pas database check"],
             "env": [{"name": "PAS_DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": "original", "key": "db"}}},
                     {"name": "PAS_ENCRYPTION_KEY", "valueFrom": {"secretKeyRef": {"name": "original", "key": "key"}}}],
             "envFrom": [{"secretRef": {"name": "original"}}], "resources": {"limits": {"cpu": "2"}},
             "volumeMounts": [{"name": "config", "mountPath": "/app/config", "readOnly": True}]}],
        "ephemeralContainers": [{"name": "old-inspector", "image": "example/old"}],
        "volumes": [{"name": "config", "configMap": {"name": "original"}}]},
        "status": {"initContainerStatuses": [{"name": "db-migrate", "imageID": "example/pas@sha256:" + "a" * 64}]}}


def render(pod, mode="inspect", name="manual-108"):
    return build_update(pod, expected_uid="a-uid", expected_image="example/pas:108",
                        name=name, mode=mode, operation_id="manual108-case")


def test_reuses_original_bootstrap_network_and_immutable_image_without_touching_init(pod):
    before = deepcopy(pod)
    update = render(pod)
    assert pod == before
    assert update["metadata"]["uid"] == "a-uid" and update["metadata"]["resourceVersion"] == "19"
    assert update["spec"]["initContainers"] == before["spec"]["initContainers"]
    assert update["spec"]["containers"] == before["spec"]["containers"]
    assert update["spec"]["volumes"] == before["spec"]["volumes"]
    assert update["spec"]["ephemeralContainers"][0] == before["spec"]["ephemeralContainers"][0]
    extra = update["spec"]["ephemeralContainers"][-1]
    assert extra["env"] == before["spec"]["initContainers"][1]["env"]
    assert extra["envFrom"] == before["spec"]["initContainers"][1]["envFrom"]
    assert extra["image"] == "example/pas@sha256:" + "a" * 64
    assert "resources" not in extra and "targetContainerName" not in extra
    assert extra["args"][0].startswith("pas database inspect --format json && python -c ")
    assert "ROOT_KEY_SHA256" in extra["args"][0]
    assert "migrate" not in extra["args"][0]


def test_migration_is_explicit_and_scoped_to_operation(pod):
    command = render(pod, mode="migrate")["spec"]["ephemeralContainers"][-1]["args"][0]
    assert "migrate --maintenance --operation-id manual108-case" in command
    assert "database check" in command


@pytest.mark.parametrize("change", ["uid", "image", "digest", "duplicate", "terminating"])
def test_refuses_drift_unknown_image_or_container_reuse(pod, change):
    if change == "uid":
        pod["metadata"]["uid"] = "another-pod"
    elif change == "image":
        pod["spec"]["initContainers"][1]["image"] = "example/pas:107"
    elif change == "digest":
        pod["status"]["initContainerStatuses"][0]["imageID"] = "unknown"
    elif change == "duplicate":
        pod["spec"]["ephemeralContainers"].append({"name": "manual-108"})
    else:
        pod["metadata"]["deletionTimestamp"] = "2026-10-19T00:00:00Z"
    with pytest.raises(ValueError):
        render(pod)
