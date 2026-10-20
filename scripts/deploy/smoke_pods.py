"""Select live, ready Deployment replicas rather than hooks or drained old Pods."""
import json
import sys


def select_application_pods(document: dict) -> list[str]:
    return [pod["metadata"]["name"] for pod in document.get("items", [])
            if not pod["metadata"].get("deletionTimestamp")
            and any(owner.get("kind") == "ReplicaSet" for owner in pod["metadata"].get("ownerReferences", []))
            and any(container.get("name") == "pas" for container in pod.get("spec", {}).get("containers", []))
            and pod.get("status", {}).get("phase") == "Running"
            and any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in pod.get("status", {}).get("conditions", []))]


if __name__ == "__main__":
    print(" ".join(select_application_pods(json.load(sys.stdin))))
