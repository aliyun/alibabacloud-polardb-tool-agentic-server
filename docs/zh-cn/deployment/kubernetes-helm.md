# 使用 Helm 部署 Kubernetes

[English](../../en/deployment/kubernetes-helm.md)

受支持的 Chart 默认部署两个 PAS 副本，不会部署元数据库。安装 Release 前先
创建数据库和 Secret。

## 创建启动 Secret

准备两个权限为 `0600` 的文件，不要把内容写入 shell 历史：

- `database-url`：完整的 `PAS_DATABASE_URL`。
- `encryption-key`：稳定的 base64 编码 32 字节根密钥。

```bash
kubectl create namespace pas-system
kubectl create secret generic pas-bootstrap \
  --namespace pas-system \
  --from-file=PAS_DATABASE_URL=./database-url \
  --from-file=PAS_ENCRYPTION_KEY=./encryption-key
```

所有迁移 Job 和应用 Pod 都读取同一个 Secret。Chart 不会创建或复制这些值。

## 安装或升级

```bash
PAS_VERSION=X.Y.Z
helm lint deploy/helm/polardb-agentic-server
helm upgrade --install pas \
  deploy/helm/polardb-agentic-server \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server \
  --set "image.tag=${PAS_VERSION}" \
  --wait --timeout 10m
```

`pre-install,pre-upgrade` 迁移 Job 与 Deployment 使用完全相同的镜像和
Secret。迁移失败时 Helm 不会更新 Deployment。应用启动还会独立检查数据库
是否达到要求的 Alembic head。

不可变部署应设置 `image.digest=sha256:...`，digest 会覆盖 tag。默认滚动策略
在一个新 Pod 启动时保持现有副本全部可用（`maxUnavailable: 0`、
`maxSurge: 1`），PDB 至少保留一个可用副本。评估环境可以设置
`replicaCount=1`，生产高可用不推荐。

按照 NOTES 输出的流程，从一个明确选定的 Pod 复制 token，不要将其打印出来。
token claim 位于共享元数据库，`/var/run/pas` 仅属于单个 Pod。

## 可选托管生命周期监听器

Chart 默认关闭管理监听器。管控系统必须分配业务与管理端口，并在渲染工作负载
时以环境变量注入；PAS 命令仍为 `serve`，不通过参数传递端口。在隔离的管控
网络中，可用如下 values 启用独立管理 Service：

```bash
helm upgrade --install pas deploy/helm/polardb-agentic-server \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set service.port=28080 \
  --set management.enabled=true \
  --set management.port=28081 \
  --set management.authMode=trusted-network \
  --set management.managedIdentity.instanceId=pmcp-example \
  --set management.managedIdentity.generation=1 \
  --set management.service.enabled=true
```

Chart 会拒绝相同的业务端口与管理端口。管理 Service 与业务 Service 分离，但
Chart 不创建 NetworkPolicy；必须增加默认拒绝策略，只允许管控命名空间和工作
负载身份访问。绝不能把该 Service 接入业务 Ingress 或负载均衡。

无法保证网络隔离时必须使用 `bearer-token`。从权限受限的文件创建 Secret，
不要把 token 放在命令行参数中，然后设置
`management.authMode=bearer-token` 与
`management.tokenSecret.name=pas-management-token`：

```bash
kubectl create secret generic pas-management-token \
  --namespace pas-system \
  --from-file=token=./management-token
```

托管初始化绝不会打印或返回 Bootstrap Token。管理员创建后的状态为
`RESET_REQUIRED`；实例激活后，由客户通过 PolarDB 客户 OpenAPI 设置首次密码。
PAS 内部接口只供生命周期管控面调用。

## 渲染清单与 `kubectl apply`

把 Helm 渲染结果交给 `kubectl apply` 时不会执行 Helm Hook 顺序，必须显式、
阻塞地完成迁移：

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

绝不能在迁移结果尚未验证时并发应用 Deployment。

## 运维

检查滚动发布和每个 Pod 的配置收敛：

```bash
kubectl rollout status \
  deployment/pas-polardb-agentic-server \
  --namespace pas-system
kubectl get pods --namespace pas-system \
  -l app.kubernetes.io/instance=pas
```

当 Pod 加载的配置版本落后于元数据库时，`/readyz` 返回 `503`，Service 不会
继续把流量路由到旧配置副本。默认配置轮询间隔为五秒。

升级前备份元数据库和根密钥。安装或升级后运行
`helm test pas --namespace pas-system`。迁移失败时先检查保留的 Hook
Pod/Job 日志，再重试。

## 受限网络中的发布烟测

Release 维护者可通过镜像仓库中的 Kind 和 MySQL 镜像执行完整双副本生命周期
测试：

```bash
PAS_VERSION=X.Y.Z
scripts/deploy/smoke-helm.sh \
  --image "polardb-agentic-server:local-v${PAS_VERSION}" \
  --mysql-image registry.example.com/library/mysql:8.0.44 \
  --kind-image registry.example.com/kindest/node:v1.35.0
```

`--kind-image` 是可选参数。当 Docker Hub 不可达，或镜像仓库中的 manifest
digest 与源仓库不同时应显式设置。脚本会创建临时 Kind 集群，并在退出时清理。
