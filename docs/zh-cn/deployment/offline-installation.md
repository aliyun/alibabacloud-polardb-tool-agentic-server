# 离线与私有镜像仓库安装

[English](../../en/deployment/offline-installation.md)

GitHub Releases 提供校验和、部署包、Helm Chart、SPDX SBOM，以及独立的 Linux
AMD64 和 ARM64 镜像归档。生产网络无法稳定访问 GHCR 时（包括许多中国大陆
环境），应使用这些资产。

## 在联网机器获取并验证

从同一个不可变 Release 下载所需资产。跨网络边界传输前先验证：

```bash
sha256sum --check SHA256SUMS
gh attestation verify \
  polardb-agentic-server-0.0.2-deploy.tar.gz \
  --repo aliyun/alibabacloud-polardb-tool-agentic-server
```

同时核对 Release 记录的镜像 manifest 与各平台 digest。不要混用不同 Release
的文件，也不要接收校验和或 attestation 验证失败的归档。

## 加载或镜像

选择与目标节点匹配的归档：

```bash
gzip --decompress --stdout \
  polardb-agentic-server-0.0.2-image-linux-amd64.tar.gz \
  | docker load
```

多主机或 Kubernetes 环境应将镜像导入客户自有 ACR、Harbor 或其他私有仓库：

```bash
docker tag SOURCE_IMAGE PRIVATE_REGISTRY/polardb-agentic-server:0.0.2
docker push PRIVATE_REGISTRY/polardb-agentic-server:0.0.2
```

推送后记录私有仓库 digest。本项目不声明不存在的中国大陆官方 ACR 地址。

## 部署

Compose 部署将 `PAS_IMAGE` 设为导入后的镜像引用，优先使用
`repository@sha256:digest`，然后按 Compose 指南操作。

Helm 部署指定私有仓库和不可变 digest：

```bash
helm upgrade --install pas ./polardb-agentic-server-0.0.2-chart.tgz \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=PRIVATE_REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:PRIVATE_REGISTRY_DIGEST
```

迁移 Job 和应用 Pod 必须使用相同的镜像 digest 与 bootstrap Secret。替换运行
中的版本前先遵循升级指南。
