# Offline and private-registry installation

[简体中文](../../zh-cn/deployment/offline-installation.md)

GitHub Releases provide checksums, a deployment bundle, the Helm Chart, an
SPDX SBOM, and separate Linux AMD64 and ARM64 image archives. Use these assets
when the production network cannot reliably reach GHCR, including many
mainland China environments.

## Acquire and verify on a connected machine

Download every required asset from the same immutable release. Verify the
checksums before moving files across the network boundary:

```bash
PAS_VERSION=0.0.3
sha256sum --check SHA256SUMS
gh attestation verify \
  "polardb-agentic-server-${PAS_VERSION}-deploy.tar.gz" \
  --repo aliyun/alibabacloud-polardb-tool-agentic-server
```

Also inspect the release's recorded image manifest and platform digests. Never
combine files from different releases or accept an archive whose checksum or
attestation does not verify.

## Load or mirror the image

Choose the archive that matches the target nodes:

```bash
gzip --decompress --stdout \
  "polardb-agentic-server-${PAS_VERSION}-image-linux-amd64.tar.gz" \
  | docker load
```

For multiple hosts or Kubernetes, import the image into a customer-controlled
ACR, Harbor, or other private registry:

```bash
docker tag SOURCE_IMAGE \
  "PRIVATE_REGISTRY/polardb-agentic-server:${PAS_VERSION}"
docker push "PRIVATE_REGISTRY/polardb-agentic-server:${PAS_VERSION}"
```

Record the private-registry digest after the push. This project does not claim
an official mainland-China ACR endpoint.

## Deploy

For Compose, set `PAS_IMAGE` to the imported image reference (prefer
`repository@sha256:digest`) and follow the Compose guide.

For Helm, set the private repository and immutable digest:

```bash
helm upgrade --install pas \
  "./polardb-agentic-server-${PAS_VERSION}-chart.tgz" \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=PRIVATE_REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:PRIVATE_REGISTRY_DIGEST
```

The migration Job and application Pods must use the same image digest and
bootstrap Secret. Follow the upgrade guide before replacing a running release.
