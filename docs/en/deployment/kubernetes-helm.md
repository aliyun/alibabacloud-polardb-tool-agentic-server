# Kubernetes deployment with Helm

[简体中文](../../zh-cn/deployment/kubernetes-helm.md)

The supported Chart deploys two PAS replicas by default and does not deploy a
metadata database. Create the database and Secret before installing the
release.

## Create the bootstrap Secret

Prepare two mode-`0600` files without writing their contents to shell history:

- `database-url`: the complete `PAS_DATABASE_URL`.
- `encryption-key`: one stable base64-encoded 32-byte root key.

```bash
kubectl create namespace pas-system
kubectl create secret generic pas-bootstrap \
  --namespace pas-system \
  --from-file=PAS_DATABASE_URL=./database-url \
  --from-file=PAS_ENCRYPTION_KEY=./encryption-key
```

Every migration Job and application Pod reads the same Secret. The Chart never
creates or copies these values.

## Install or upgrade

```bash
PAS_VERSION=0.0.6
helm lint deploy/helm/polardb-agentic-server
helm upgrade --install pas \
  deploy/helm/polardb-agentic-server \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server \
  --set "image.tag=${PAS_VERSION}" \
  --wait --timeout 10m
```

The `pre-install,pre-upgrade` migration Job uses the exact image and Secret
used by the Deployment. Helm does not update the Deployment when migration
fails. Application startup independently checks that the database reached the
required Alembic head.

For an immutable deployment, set `image.digest=sha256:...`; a digest overrides
the tag. The default rolling strategy keeps all existing replicas available
while one new Pod starts (`maxUnavailable: 0`, `maxSurge: 1`). The PDB keeps
at least one replica available. `replicaCount=1` is supported for evaluation,
but not recommended for production availability.

Run the printed NOTES bootstrap procedure to copy the token from one selected
Pod without printing it. The token claim is shared in the metadata database,
while `/var/run/pas` is Pod-local.

## Rendered manifests and `kubectl apply`

Helm hook ordering does not run when its rendered YAML is passed to
`kubectl apply`. Run migration as an explicit, blocking step:

```bash
helm template pas deploy/helm/polardb-agentic-server \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --show-only templates/migration-job.yaml \
  > pas-migration.yaml

kubectl delete job pas-polardb-agentic-server-migrate \
  --namespace pas-system --ignore-not-found
kubectl apply --namespace pas-system -f pas-migration.yaml
kubectl wait --namespace pas-system \
  --for=condition=complete \
  job/pas-polardb-agentic-server-migrate \
  --timeout=10m

helm template pas deploy/helm/polardb-agentic-server \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set migration.enabled=false \
  > pas-release.yaml
kubectl apply --namespace pas-system -f pas-release.yaml
```

Never apply the Deployment concurrently with an unverified migration.

## Operations

Check rollout and per-Pod configuration convergence:

```bash
kubectl rollout status \
  deployment/pas-polardb-agentic-server \
  --namespace pas-system
kubectl get pods --namespace pas-system \
  -l app.kubernetes.io/instance=pas
```

`/readyz` returns `503` while a Pod's loaded configuration version is behind
the metadata database, so Services stop routing to a stale replica. The
default configuration poll interval is five seconds.

Before upgrade, back up the metadata database and root key. Run
`helm test pas --namespace pas-system` after install or upgrade. Inspect a
failed migration with the retained failed hook Pod/Job logs before retrying.

## Release smoke test in restricted networks

Release maintainers can run the complete two-replica lifecycle test with
mirrored Kind and MySQL images:

```bash
PAS_VERSION=0.0.6
scripts/deploy/smoke-helm.sh \
  --image "polardb-agentic-server:local-v${PAS_VERSION}" \
  --mysql-image registry.example.com/library/mysql:8.0.44 \
  --kind-image registry.example.com/kindest/node:v1.35.0
```

`--kind-image` is optional. Set it when Docker Hub is unavailable or when the
mirror has a registry-specific manifest digest. The script creates a temporary
Kind cluster and removes it on exit.
