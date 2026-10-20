from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


@pytest.mark.parametrize("mode", ["trusted-network", "bearer-token"])
@pytest.mark.parametrize("service_type", ["ClusterIP", "NodePort", "LoadBalancer"])
def test_management_service_cannot_be_public(mode, service_type):
    if shutil.which("helm") is None:
        pytest.skip("Helm is not installed")
    chart = Path(__file__).resolve().parents[1] / "deploy/helm/polardb-agentic-server"
    result = subprocess.run([
        "helm", "template", "pas", str(chart), "--set",
        ",".join([
            "existingSecret=pas-bootstrap", "management.enabled=true",
            "management.port=28081", f"management.authMode={mode}",
            "management.managedIdentity.instanceId=pmcp-example",
            "management.managedIdentity.generation=1",
            "management.tokenSecret.name=management-credentials",
            "management.service.enabled=true", f"management.service.type={service_type}",
        ]),
    ], text=True, capture_output=True, check=False)
    if service_type != "ClusterIP":
        assert result.returncode != 0
        assert "management.service.type must be ClusterIP" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        documents = list(yaml.safe_load_all(result.stdout))
        services = [d for d in documents if d and d["kind"] == "Service"]
        assert len(services) == 2
        assert all(len(d["spec"]["ports"]) == 1 for d in services)
        deployment = next(d for d in documents if d and d["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["PAS_MANAGEMENT_AUTH_MODE"] == mode
        assert container["livenessProbe"]["httpGet"]["path"] == "/livez"
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
