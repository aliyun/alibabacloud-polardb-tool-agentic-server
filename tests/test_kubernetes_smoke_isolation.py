"""Protect caller contexts and existing clusters before real kind acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_smoke(tmp_path, *, occupied=False, api_version=None):
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "commands.jsonl"
    caller_config = tmp_path / "caller-kubeconfig"
    caller_config.write_text("user-owned-context\n")
    program = f"#!{sys.executable}\n" + '''import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name
with open(os.environ['SMOKE_COMMAND_LOG'],'a') as stream:
 stream.write(json.dumps({'command':name,'args':sys.argv[1:],'kubeconfig':os.environ.get('KUBECONFIG')})+'\\n')
if name=='kind' and sys.argv[1:3]==['get','clusters']:
 print(os.environ.get('SMOKE_EXISTING_CLUSTER',''))
elif name=='kind' and sys.argv[1:3]==['create','cluster']:
 sys.exit(0 if os.environ.get('SMOKE_API_VERSION') else 31)
elif name=='kind' and sys.argv[1:3]==['load','docker-image'] and os.environ.get('SMOKE_API_VERSION')=='1.54':
 sys.exit('partial multi-platform cache')
elif name=='docker' and sys.argv[1]=='version':
 print(os.environ['SMOKE_API_VERSION'])
elif name=='docker' and sys.argv[1:3]==['image','inspect']:
 print('linux/amd64' if 'pas-local:owned' in sys.argv else 'linux/arm64')
elif name=='docker' and sys.argv[1:3]==['image','save']:
 Path(sys.argv[sys.argv.index('--output')+1]).write_text('owned archive')
elif name=='kind' and sys.argv[1:3]==['load','image-archive']:
 assert Path(sys.argv[3]).is_file()
elif name=='kubectl' and sys.argv[1:3]==['create','namespace']:
 sys.exit(29)
'''
    for name in ("kind", "kubectl", "helm", "docker"):
        p = binary / name
        p.write_text(program)
        p.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "scripts/deploy/smoke-helm.sh"),
        "--image", "pas-local:owned", "--mysql-image", "mysql:owned",
        "--cluster-name", "pas-release-smoke-occupied"], cwd=ROOT,
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "KUBECONFIG": str(caller_config),
             "SMOKE_COMMAND_LOG": str(log),
             "SMOKE_API_VERSION": api_version or "",
             "SMOKE_EXISTING_CLUSTER": "pas-release-smoke-occupied" if occupied else ""},
        capture_output=True, text=True, check=False)
    commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert caller_config.read_text() == "user-owned-context\n"
    return result, commands, caller_config


def test_smoke_refuses_existing_cluster_without_mutation(tmp_path):
    result, commands, _ = run_smoke(tmp_path, occupied=True)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert all(command["command"] == "kind" and command["args"] == ["get", "clusters"] for command in commands)


def test_failed_cluster_creation_cleans_only_owned_cluster_and_private_config(tmp_path):
    result, commands, caller_config = run_smoke(tmp_path)
    assert result.returncode == 31
    assert not any(command["command"] in {"helm", "kubectl"} for command in commands)
    create = next(command for command in commands if command["args"][:2] == ["create", "cluster"])
    assert create["kubeconfig"] != str(caller_config)
    assert "--kubeconfig" in create["args"]
    private = create["args"][create["args"].index("--kubeconfig") + 1]
    assert not Path(private).exists()
    assert any(command["args"] == ["delete", "cluster", "--name", "pas-release-smoke-occupied"] for command in commands)


@pytest.mark.parametrize("api_version", ["1.54", "1.47"])
def test_image_preload_handles_partial_platform_cache_and_legacy_engine(tmp_path, api_version):
    result, commands, _ = run_smoke(tmp_path, api_version=api_version)
    assert result.returncode == 29, result.stderr
    archives = [command for command in commands if command["args"][:2] == ["load", "image-archive"]]
    if api_version == "1.54":
        assert len(archives) == 2
        saves = [command["args"] for command in commands if command["args"][:2] == ["image", "save"]]
        assert [args[args.index("--platform") + 1] for args in saves] == ["linux/amd64", "linux/arm64"]
        assert all(not Path(command["args"][2]).exists() for command in archives)
    else:
        assert not archives
        assert len([command for command in commands if command["args"][:2] == ["load", "docker-image"]]) == 2


def test_replica_selection_excludes_terminating_and_hook_pods_after_rollout():
    from scripts.deploy.smoke_pods import select_application_pods

    def pod(name, *, owner="ReplicaSet", ready=True, deleting=False):
        return {"metadata": {"name": name, "ownerReferences": [{"kind": owner}],
                **({"deletionTimestamp": "2026-10-16T00:00:00Z"} if deleting else {})},
                "spec": {"containers": [{"name": "pas" if owner == "ReplicaSet" else "request"}]},
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": str(ready)}]}}

    document = {"items": [pod("new-a"), pod("old-terminating", deleting=True), pod("migration", owner="Job"),
                          pod("new-b"), pod("helm-test", owner=""), pod("unready", ready=False)]}
    assert select_application_pods(document) == ["new-a", "new-b"]
