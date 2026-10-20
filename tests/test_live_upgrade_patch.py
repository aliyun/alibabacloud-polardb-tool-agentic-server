"""Safety boundaries for the explicit live-peer operator exception."""
from copy import deepcopy

import pytest

from scripts.dev.live_upgrade_patch import build_delete_options, build_patch


@pytest.fixture
def statefulset():
    return {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"uid": "owned-sts-uid", "resourceVersion": "17", "name": "test-a"},
        "spec": {"replicas": 1, "updateStrategy": {"type": "OnDelete"}, "template": {"spec": {
            "containers": [{"name": "mcp-server", "image": "example/pas:108", "envFrom": [{"secretRef": {"name": "original"}}]}],
            "initContainers": [
                {"name": "restore-ipflt", "image": "example/whiteip:original"},
                {"name": "db-migrate", "image": "example/pas:108", "command": ["/bin/sh", "-c"],
                 "args": ["pas database migrate && exec pas database check"], "resources": {"requests": {"cpu": "2"}}},
            ], "volumes": [{"name": "config", "configMap": {"name": "original"}}],
        }}},
    }


def render(sts, mode="override", **kwargs):
    return build_patch(sts, expected_uid="owned-sts-uid", expected_image="example/pas:108",
                       operation_id=kwargs.pop("operation_id", "live-108-case"), mode=mode, **kwargs)


def test_only_init_arguments_change_with_server_identity_preconditions(statefulset):
    before = deepcopy(statefulset)
    patch = render(statefulset)
    assert statefulset == before
    preconditions = {p["path"]: p["value"] for p in patch if p["op"] == "test"}
    assert preconditions["/metadata/uid"] == "owned-sts-uid"
    assert preconditions["/metadata/resourceVersion"] == "17"
    changes = [p for p in patch if p["op"] != "test"]
    assert len(changes) == 1 and changes[0]["path"] == "/spec/template/spec/initContainers/1/args"
    command = changes[0]["value"][0]
    assert command.count("migrate --maintenance --operation-id live-108-case") == 2
    assert command.count("database check") == 2


def test_restore_and_replay_keep_exact_original_command(statefulset):
    original = deepcopy(statefulset["spec"]["template"]["spec"]["initContainers"][1]["args"])
    override = render(statefulset)[-1]["value"]
    statefulset["spec"]["template"]["spec"]["initContainers"][1]["args"] = override
    assert all(p["op"] == "test" for p in render(statefulset))
    assert render(statefulset, mode="restore")[-1]["value"] == original
    statefulset["spec"]["template"]["spec"]["initContainers"][1]["args"] = original
    assert all(p["op"] == "test" for p in render(statefulset, mode="restore"))


@pytest.mark.parametrize("field,value", [("uid", "different-sts"), ("resourceVersion", "")])
def test_reject_wrong_or_unversioned_resource(statefulset, field, value):
    statefulset["metadata"][field] = value
    with pytest.raises(ValueError):
        render(statefulset)


@pytest.mark.parametrize("target", ["containers", "initContainers"])
def test_reject_image_changes(statefulset, target):
    statefulset["spec"]["template"]["spec"][target][-1]["image"] = "example/pas:other"
    with pytest.raises(ValueError):
        render(statefulset)


@pytest.mark.parametrize("operation", [";touch /tmp/should-not-run", "$(id)", "x && true", "x\ny", "", "x" * 129])
def test_operation_cannot_inject_shell_code(statefulset, operation):
    with pytest.raises(ValueError):
        render(statefulset, operation_id=operation)


def test_reject_unknown_shell_or_migration_commands(statefulset):
    init = statefulset["spec"]["template"]["spec"]["initContainers"][1]
    init["args"] = ["pas database migrate --unknown-option"]
    with pytest.raises(ValueError):
        render(statefulset)
    init["args"] = ["pas database migrate && exec pas database check"]
    init["command"] = ["bash", "-c"]
    with pytest.raises(ValueError):
        render(statefulset)


def test_reject_automatic_rollout_or_another_topology(statefulset):
    statefulset["spec"]["updateStrategy"]["type"] = "RollingUpdate"
    with pytest.raises(ValueError):
        render(statefulset)
    statefulset["spec"]["updateStrategy"]["type"] = "OnDelete"
    statefulset["spec"]["replicas"] = 2
    with pytest.raises(ValueError):
        render(statefulset)


@pytest.mark.parametrize("mismatch", [None, "owner", "image", "uid", "terminating"])
def test_delete_uses_server_preconditions_and_cannot_target_peer(mismatch):
    pod = {"kind": "Pod", "metadata": {"uid": "a-pod-uid", "resourceVersion": "24",
        "ownerReferences": [{"kind": "StatefulSet", "uid": "a-sts-uid", "controller": True}]},
        "spec": {"containers": [{"name": "mcp-server", "image": "example/pas:108"}]}}
    if mismatch == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "b-sts-uid"
    elif mismatch == "image":
        pod["spec"]["containers"][0]["image"] = "example/pas:107"
    elif mismatch == "uid":
        pod["metadata"]["uid"] = ""
    elif mismatch == "terminating":
        pod["metadata"]["deletionTimestamp"] = "2026-10-19T00:00:00Z"
    if mismatch:
        with pytest.raises(ValueError):
            build_delete_options(pod, expected_uid="a-sts-uid", expected_image="example/pas:108")
    else:
        result = build_delete_options(pod, expected_uid="a-sts-uid", expected_image="example/pas:108")
        assert result["preconditions"] == {"uid": "a-pod-uid", "resourceVersion": "24"}
