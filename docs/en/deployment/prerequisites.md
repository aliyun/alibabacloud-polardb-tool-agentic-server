# Production deployment prerequisites

[简体中文](../../zh-cn/deployment/prerequisites.md)

This is an early public trial release. Use it in a controlled environment,
keep backups, and pin the deployed image by digest.

## Supported runtime

- Linux `amd64` and `arm64`.
- Docker Engine 24 or later and Docker Compose v2.20 or later for the
  supported single-host deployment.
- Kubernetes 1.27 or later and Helm 3.12 or later for the supported
  multi-replica deployment.
- A MySQL 8.0 or PostgreSQL metadata database reachable from every backend
  Pod. PolarDB for MySQL 8.0 is recommended for ACK production deployments.

The container runs as UID and GID `10001`, listens on TCP `18760`, and needs
writable mounts for `/tmp`, `/app/log`, and `/var/run/pas`. Its root
filesystem can be read-only.

## Required bootstrap settings

Every migration command and application replica requires the same two
settings:

- `PAS_DATABASE_URL`: metadata database connection URL.
- `PAS_ENCRYPTION_KEY`: stable base64-encoded 32-byte root encryption key.

Generate and store the root key once in a secret manager. Do not rotate it by
restarting a container and do not print it to CI or container logs. Changing
or losing it makes encrypted configuration unreadable.

Run `pas database migrate` once before starting a new application version.
Application Pods only run the read-only `pas database check` startup gate and
never migrate automatically.

## Image registry access

The default examples use GitHub Container Registry (GHCR). Networks in
Mainland China may need an approved registry proxy or a private Alibaba Cloud
Container Registry mirror. Mirror the exact release digest and configure
Compose or Helm to use that mirror; do not replace a version with a floating
tag.

The container image also provides a mutable `latest` alias for evaluation.
Do not use it for production: pin the semantic version shown below or the
verified digest instead.

Verify an image after mirroring:

```bash
PAS_VERSION=0.0.3
docker buildx imagetools inspect \
  "ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:${PAS_VERSION}"
```

Continue with the Docker Compose or Kubernetes/Helm deployment guide after
these prerequisites are satisfied.
