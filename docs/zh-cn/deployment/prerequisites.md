# 生产部署前提

[English](../../en/deployment/prerequisites.md)

当前为公开试用版本。请在受控环境使用，保留备份，并按镜像 digest 固定
实际部署内容。

## 支持的运行环境

- Linux `amd64` 与 `arm64`。
- 单机部署使用 Docker Engine 24 或更高版本，以及 Docker Compose v2.20
  或更高版本。
- 多副本部署使用 Kubernetes 1.27 或更高版本，以及 Helm 3.12 或更高版本。
- 所有后端 Pod 均可访问的 MySQL 8.0 或 PostgreSQL 元数据库。ACK 生产部署
  推荐使用 PolarDB MySQL 8.0。

容器以 UID/GID `10001` 运行，监听 TCP `18760`，并要求 `/tmp`、
`/app/log`、`/var/run/pas` 可写；根文件系统可以设置为只读。

## 必需的启动配置

迁移命令和每个应用副本都必须使用相同的两个配置：

- `PAS_DATABASE_URL`：元数据库连接 URL。
- `PAS_ENCRYPTION_KEY`：稳定的 base64 编码 32 字节根加密密钥。

根密钥只生成一次并存入密钥管理系统。不要通过重启容器轮换密钥，也不要把
密钥输出到 CI 或容器日志。密钥变更或丢失会导致已加密配置无法读取。

每次部署新应用版本前，仅执行一次 `pas database migrate`。应用 Pod 启动时
只执行只读的 `pas database check` 门禁，绝不会自动迁移。

## 镜像仓库访问

默认示例使用 GitHub Container Registry（GHCR）。中国大陆网络可能需要合规
的镜像代理，或同步到私有阿里云容器镜像服务。请同步同一个发布 digest，并在
Compose 或 Helm 中指定镜像仓库；不要用浮动标签替代固定版本。

容器镜像同时提供可变的 `latest` 别名供试用。生产环境不要使用该别名，应固定
下方所示的语义版本，或直接固定已验证的 digest。

同步后可检查镜像：

```bash
PAS_VERSION=0.0.3
docker buildx imagetools inspect \
  "ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:${PAS_VERSION}"
```

满足以上前提后，再继续 Docker Compose 或 Kubernetes/Helm 部署指南。
