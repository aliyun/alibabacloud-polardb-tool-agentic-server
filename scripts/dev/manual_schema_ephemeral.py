"""Render an A-Pod ephemeral-container update; never change its init/template."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex


def build_update(pod: dict, *, expected_uid: str, expected_image: str,
                 name: str, mode: str, operation_id: str) -> dict:
    meta = pod.get("metadata", {})
    if (pod.get("kind") != "Pod" or not expected_uid or meta.get("uid") != expected_uid
            or not meta.get("resourceVersion") or meta.get("deletionTimestamp")):
        raise ValueError("A current, explicitly identified Pod is required")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("Use a unique DNS container name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id):
        raise ValueError("A stable shell-safe operation ID is required")
    spec = pod["spec"]
    containers = spec.get("containers", []) + spec.get("initContainers", []) + spec.get("ephemeralContainers", [])
    if any(c.get("name") == name for c in containers):
        raise ValueError("Preserve existing ephemeral evidence; choose a new container name")
    inits = [c for c in spec.get("initContainers", []) if c.get("name") == "db-migrate"]
    business = [c for c in spec.get("containers", []) if c.get("name") == "mcp-server"]
    if len(inits) != 1 or len(business) != 1 or any(c.get("image") != expected_image for c in inits + business):
        raise ValueError("Original init/business images must match the recorded target")
    init = inits[0]
    statuses = [s for s in pod.get("status", {}).get("initContainerStatuses", []) if s.get("name") == "db-migrate"]
    image = statuses[0].get("imageID", "").removeprefix("docker-pullable://") if len(statuses) == 1 else ""
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("The original init's immutable pullable image ID is required")
    if mode == "inspect":
        fingerprint = ("import hashlib; from server.bootstrap import load_bootstrap_settings; "
                       "print('ROOT_KEY_SHA256='+hashlib.sha256(load_bootstrap_settings().encryption_key).hexdigest())")
        command = "pas database inspect --format json && python -c " + shlex.quote(fingerprint)
    elif mode == "migrate":
        command = f"pas database migrate --maintenance --operation-id {operation_id} && exec pas database check --format json"
    else:
        raise ValueError("Unknown mode")
    extra = {field: deepcopy(init[field]) for field in ("workingDir", "env", "envFrom", "volumeMounts", "securityContext") if field in init}
    extra.update(name=name, image=image, imagePullPolicy="IfNotPresent", command=["/bin/sh", "-ec"], args=[command])
    result = deepcopy(pod)
    result.pop("status", None)
    result["spec"]["ephemeralContainers"] = deepcopy(spec.get("ephemeralContainers", [])) + [extra]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-file", type=Path, required=True)
    parser.add_argument("--expected-uid", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--mode", choices=("inspect", "migrate"), required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    try:
        result = build_update(json.loads(args.pod_file.read_text()), expected_uid=args.expected_uid,
            expected_image=args.expected_image, name=args.name, mode=args.mode, operation_id=args.operation_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
