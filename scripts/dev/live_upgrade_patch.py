"""Render an offline, identity-guarded init patch for an operator-approved exception.

This tool never connects to Kubernetes or a database. The normal PAS migration
guard and Provider code are unchanged. See the live-peer exception runbook.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ORIGINAL = "pas database migrate && exec pas database check"


def override_command(operation_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id):
        raise ValueError("Use a stable, shell-safe operation ID of at most 128 characters")
    migrate = f"pas database migrate --maintenance --operation-id {operation_id}"
    return f"{migrate} && pas database check && {migrate} && exec pas database check"


def build_patch(sts: dict, *, expected_uid: str, expected_image: str,
                operation_id: str, mode: str) -> list[dict]:
    temporary = override_command(operation_id)
    if mode not in {"override", "restore"}:
        raise ValueError("Unknown patch mode")
    meta = sts.get("metadata", {})
    if (sts.get("apiVersion") != "apps/v1" or sts.get("kind") != "StatefulSet"
            or not expected_uid or meta.get("uid") != expected_uid or not meta.get("resourceVersion")):
        raise ValueError("An explicitly identified, versioned StatefulSet is required")
    spec = sts.get("spec", {})
    if spec.get("replicas") != 1 or spec.get("updateStrategy", {}).get("type") != "OnDelete":
        raise ValueError("Only the existing single-replica OnDelete topology is supported")
    pod = spec["template"]["spec"]
    business = [c for c in pod.get("containers", []) if c.get("name") == "mcp-server"]
    matches = [(i, c) for i, c in enumerate(pod.get("initContainers", [])) if c.get("name") == "db-migrate"]
    if len(business) != 1 or len(matches) != 1 or not expected_image:
        raise ValueError("One mcp-server and one db-migrate container are required")
    index, init = matches[0]
    if init.get("image") != expected_image or business[0].get("image") != expected_image:
        raise ValueError("Both container images must match the recorded target exactly")
    if init.get("command") != ["/bin/sh", "-c"] or init.get("args") not in ([ORIGINAL], [temporary]):
        raise ValueError("Refusing an unknown init command or another operation's override")
    base = f"/spec/template/spec/initContainers/{index}"
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": base + "/args", "value": list(init["args"])},
    ]
    desired = [temporary if mode == "override" else ORIGINAL]
    if init["args"] != desired:
        patch.append({"op": "replace", "path": base + "/args", "value": desired})
    return patch


def build_delete_options(pod: dict, *, expected_uid: str, expected_image: str) -> dict:
    """Bind deletion to A's controller UID, exact image, and observed Pod UID."""
    meta = pod.get("metadata", {})
    owners = [o for o in meta.get("ownerReferences", [])
              if o.get("controller") is True and o.get("kind") == "StatefulSet" and o.get("uid") == expected_uid]
    business = [c for c in pod.get("spec", {}).get("containers", []) if c.get("name") == "mcp-server"]
    if (pod.get("kind") != "Pod" or not meta.get("uid") or not meta.get("resourceVersion")
            or meta.get("deletionTimestamp") or not expected_uid or len(owners) != 1
            or len(business) != 1 or not expected_image or business[0].get("image") != expected_image):
        raise ValueError("Refusing deletion without A's controller, image and current Pod identity")
    return {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background",
            "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--statefulset-file", type=Path)
    parser.add_argument("--pod-file", type=Path)
    parser.add_argument("--expected-uid", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--mode", choices=("override", "restore", "delete-options"), required=True)
    args = parser.parse_args()
    try:
        if args.mode == "delete-options":
            if args.pod_file is None:
                raise ValueError("--pod-file is required")
            patch = build_delete_options(json.loads(args.pod_file.read_text()), expected_uid=args.expected_uid,
                                         expected_image=args.expected_image)
        else:
            if args.statefulset_file is None or args.operation_id is None:
                raise ValueError("--statefulset-file and --operation-id are required")
            patch = build_patch(json.loads(args.statefulset_file.read_text()), expected_uid=args.expected_uid,
                expected_image=args.expected_image, operation_id=args.operation_id, mode=args.mode)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(patch, indent=2))


if __name__ == "__main__":
    main()
