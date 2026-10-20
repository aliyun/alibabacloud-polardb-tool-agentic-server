#!/usr/bin/env bash
set -euo pipefail

PAS_IMAGE=""
MYSQL_IMAGE=""
KIND_IMAGE=""
CLUSTER_NAME="pas-release-smoke-$(date +%s)-${RANDOM}"
NAMESPACE="pas-system"
RELEASE="pas"
CHART="deploy/helm/polardb-agentic-server"
SMOKE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      PAS_IMAGE="$2"
      shift 2
      ;;
    --mysql-image)
      MYSQL_IMAGE="$2"
      shift 2
      ;;
    --kind-image)
      KIND_IMAGE="$2"
      shift 2
      ;;
    --cluster-name)
      CLUSTER_NAME="$2"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PAS_IMAGE" || -z "$MYSQL_IMAGE" ]]; then
  echo "usage: $0 --image IMAGE --mysql-image MYSQL_IMAGE [--kind-image IMAGE]" >&2
  exit 2
fi
if [[ "$PAS_IMAGE" != *:* || "$PAS_IMAGE" == *@* ]]; then
  echo "--image must be a locally loadable repository:tag" >&2
  exit 2
fi
for command_name in docker kind kubectl helm python3; do
  command -v "$command_name" >/dev/null
done
if [[ ! "$CLUSTER_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#CLUSTER_NAME} -gt 63 ]]; then
  echo "invalid local kind cluster name" >&2
  exit 2
fi
EXISTING_CLUSTERS="$(kind get clusters)"
while IFS= read -r existing_cluster; do
  if [[ "$existing_cluster" == "$CLUSTER_NAME" ]]; then
    echo "kind cluster already exists: $CLUSTER_NAME" >&2
    exit 2
  fi
done <<<"$EXISTING_CLUSTERS"

SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pas-helm-smoke.XXXXXX")"
export KUBECONFIG="$SMOKE_ROOT/kubeconfig"
CLUSTER_OWNED=false
CLUSTER_READY=false

cleanup() {
  local result=$?
  trap - EXIT
  if [[ "$CLUSTER_READY" == true ]]; then
    if [[ "$result" != 0 ]]; then
      kubectl get pods,jobs,services --namespace "$NAMESPACE" -o wide >&2 || true
      kubectl get events --namespace "$NAMESPACE" --sort-by=.lastTimestamp >&2 || true
      kubectl logs "job/${RELEASE}-polardb-agentic-server-migrate" \
        --namespace "$NAMESPACE" --all-containers --tail=100 >&2 || true
    fi
    helm uninstall "$RELEASE" --namespace "$NAMESPACE" >/dev/null 2>&1 || true
  fi
  if [[ "$CLUSTER_OWNED" == true ]]; then
    kind delete cluster --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$SMOKE_ROOT"
  exit "$result"
}
trap cleanup EXIT

kind_create_args=(--name "$CLUSTER_NAME" --wait 120s --kubeconfig "$KUBECONFIG")
if [[ -n "$KIND_IMAGE" ]]; then
  kind_create_args+=(--image "$KIND_IMAGE")
fi
CLUSTER_OWNED=true
kind create cluster "${kind_create_args[@]}"
CLUSTER_READY=true

load_local_image() {
  local image="$1"
  local platform
  if docker version --format '{{.Server.APIVersion}}' | python3 -c \
    'import sys; raise SystemExit(tuple(map(int, sys.stdin.read().strip().split("."))) < (1, 48))'; then
    # Containerd-backed Docker stores may have only one platform of an index.
    # Export the locally available platform so ctr does not require other blobs.
    platform="$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')"
    docker image save --platform "$platform" --output "$SMOKE_ROOT/image.tar" "$image"
    kind load image-archive "$SMOKE_ROOT/image.tar" --name "$CLUSTER_NAME"
    rm "$SMOKE_ROOT/image.tar"
  else
    kind load docker-image "$image" --name "$CLUSTER_NAME"
  fi
}
load_local_image "$PAS_IMAGE"
if ! docker image inspect "$MYSQL_IMAGE" >/dev/null 2>&1; then
  docker pull "$MYSQL_IMAGE"
fi
load_local_image "$MYSQL_IMAGE"
kubectl create namespace "$NAMESPACE"

MYSQL_ROOT_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
MYSQL_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
PAS_ENCRYPTION_KEY="$(python3 -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())')"

kubectl create secret generic mysql-bootstrap \
  --namespace "$NAMESPACE" \
  --from-literal=MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  --from-literal=MYSQL_PASSWORD="$MYSQL_PASSWORD"
kubectl apply --namespace "$NAMESPACE" -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: mysql
spec:
  selector:
    app: mysql
  ports:
    - port: 3306
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mysql
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mysql
  template:
    metadata:
      labels:
        app: mysql
    spec:
      containers:
        - name: mysql
          image: ${MYSQL_IMAGE}
          env:
            - name: MYSQL_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mysql-bootstrap
                  key: MYSQL_ROOT_PASSWORD
            - name: MYSQL_DATABASE
              value: polardb_agentic
            - name: MYSQL_USER
              value: pas
            - name: MYSQL_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mysql-bootstrap
                  key: MYSQL_PASSWORD
          ports:
            - containerPort: 3306
          readinessProbe:
            exec:
              command:
                - sh
                - -c
                - MYSQL_PWD="\$MYSQL_ROOT_PASSWORD" mysqladmin ping -h 127.0.0.1 -u root --silent
            initialDelaySeconds: 10
            periodSeconds: 3
EOF
kubectl rollout status deployment/mysql \
  --namespace "$NAMESPACE" --timeout=180s

DATABASE_URL="mysql+asyncmy://pas:${MYSQL_PASSWORD}@mysql:3306/polardb_agentic"
kubectl create secret generic pas-bootstrap \
  --namespace "$NAMESPACE" \
  --from-literal=PAS_DATABASE_URL="$DATABASE_URL" \
  --from-literal=PAS_ENCRYPTION_KEY="$PAS_ENCRYPTION_KEY"

IMAGE_REPOSITORY="${PAS_IMAGE%:*}"
IMAGE_TAG="${PAS_IMAGE##*:}"
helm install "$RELEASE" "$CHART" \
  --namespace "$NAMESPACE" \
  --set existingSecret=pas-bootstrap \
  --set image.repository="$IMAGE_REPOSITORY" \
  --set image.tag="$IMAGE_TAG" \
  --set image.pullPolicy=IfNotPresent \
  --wait --timeout 5m
kubectl rollout status \
  "deployment/${RELEASE}-polardb-agentic-server" \
  --namespace "$NAMESPACE" --timeout=180s

application_pods() {
  kubectl get pods --namespace "$NAMESPACE" \
    -l app.kubernetes.io/instance="$RELEASE",app.kubernetes.io/name=polardb-agentic-server \
    -o json | python3 "$SMOKE_SCRIPT_DIR/smoke_pods.py"
}
READY_PODS="$(application_pods)"
if [[ "$(wc -w <<<"$READY_PODS" | tr -d ' ')" != "2" ]]; then
  echo "expected two running PAS Pods" >&2
  exit 1
fi
SETUP_POD="${READY_PODS%% *}"
kubectl exec --namespace "$NAMESPACE" "$SETUP_POD" -- \
  pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token
kubectl exec --namespace "$NAMESPACE" -i "$SETUP_POD" -- python - <<'PY'
import json
import urllib.request

token = open("/var/run/pas/bootstrap-token", encoding="utf-8").read().strip()

def request(body):
    value = urllib.request.Request(
        "http://127.0.0.1:18760/api/config",
        data=json.dumps({"protocol_version": 1, **body}).encode(),
        headers={
            "Authorization": f"Bootstrap {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(value, timeout=20) as response:
        return json.load(response)

request({
    "action": "plan",
    "module": "core_admin",
    "config": {
        "username": "admin",
        "password": "smoke-test-password-only",
    },
})
saved = request({
    "action": "save_draft",
    "module": "core_admin",
    "expected_revision": 0,
    "config": {"username": "admin"},
})
validated = request({
    "action": "validate",
    "module": "core_admin",
    "expected_revision": saved["module"]["revision"],
})
request({
    "action": "activate",
    "module": "core_admin",
    "expected_revision": validated["module"]["revision"],
    "validation_id": validated["validation"]["validation_id"],
    "idempotency_key": "kind-release-smoke-core-admin",
    "config": {"password": "smoke-test-password-only"},
})
PY

deadline=$((SECONDS + 30))
for pod in $READY_PODS; do
  until kubectl exec --namespace "$NAMESPACE" "$pod" -- python -c \
    'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:18760/readyz")); assert d["mode"]=="READY" and d["config_status"]=="CURRENT"'; do
    if (( SECONDS >= deadline )); then
      echo "configuration did not converge on $pod" >&2
      exit 1
    fi
    sleep 1
  done
done

authenticated_replica_smoke() {
  local phase="$1"
  local pods pod
  pods="$(application_pods)"
  if [[ "$(wc -w <<<"$pods" | tr -d ' ')" != 2 ]]; then
    echo "expected two PAS replicas during $phase" >&2
    return 1
  fi
  for pod in $pods; do
    kubectl exec --namespace "$NAMESPACE" "$pod" -- pas database check
    kubectl exec --namespace "$NAMESPACE" -i "$pod" -- python - "$phase" <<'PY'
import http.cookiejar
import json
import sys
import urllib.request
import uuid

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def request(path, data=None):
    value = urllib.request.Request("http://127.0.0.1:18760" + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "X-PAS-CSRF": "1"})
    with opener.open(value, timeout=20) as response:
        return json.load(response)

request("/auth/login", {"username": "admin", "password": "smoke-test-password-only"})
assert request("/auth/me")["role"] == "admin"
request("/api/users")
if sys.argv[1] != "installed":
    agents = request("/api/agents")
    assert any(item["name"].startswith("Helm smoke installed ") for item in agents["items"])
name = "Helm smoke " + sys.argv[1] + " " + uuid.uuid4().hex[:8]
created = request("/api/agents", {"name": name})
assert created["name"] == name
print("Authenticated PAS read/write smoke passed: " + sys.argv[1])
PY
  done
}

authenticated_replica_smoke installed
helm test "$RELEASE" --namespace "$NAMESPACE" --logs --timeout 2m
helm upgrade "$RELEASE" "$CHART" \
  --namespace "$NAMESPACE" \
  --reuse-values \
  --set-string podAnnotations.release-smoke="$(date +%s)" \
  --wait --timeout 5m
kubectl rollout status \
  "deployment/${RELEASE}-polardb-agentic-server" \
  --namespace "$NAMESPACE" --timeout=180s
authenticated_replica_smoke rolled-out

generation_before="$(kubectl get deployment \
  "${RELEASE}-polardb-agentic-server" --namespace "$NAMESPACE" \
  -o jsonpath='{.metadata.generation}')"
if helm upgrade "$RELEASE" "$CHART" \
  --namespace "$NAMESPACE" \
  --reuse-values \
  --set 'migration.args[0]=invalid-command' \
  --wait --timeout 90s; then
  echo "deliberately failing migration unexpectedly succeeded" >&2
  exit 1
fi
generation_after="$(kubectl get deployment \
  "${RELEASE}-polardb-agentic-server" --namespace "$NAMESPACE" \
  -o jsonpath='{.metadata.generation}')"
if [[ "$generation_before" != "$generation_after" ]]; then
  echo "failed migration changed the Deployment" >&2
  exit 1
fi
authenticated_replica_smoke migration-refused

helm upgrade "$RELEASE" "$CHART" \
  --namespace "$NAMESPACE" \
  --reuse-values \
  --set 'migration.args[0]=database' \
  --set 'migration.args[1]=migrate' \
  --wait --timeout 5m
authenticated_replica_smoke recovered
echo "Helm release smoke test passed"
