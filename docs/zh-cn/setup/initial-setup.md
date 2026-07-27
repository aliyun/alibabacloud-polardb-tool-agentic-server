# 初始化设置

[English](../../en/setup/initial-setup.md) | **简体中文**

本文介绍服务首次启动、bootstrap token 交付、首个管理员创建，以及原 token
不可用时的恢复方式。完成接管后，再继续进行引导式模块化配置。

## 启动配置

进程只接受两个启动配置：

- `PAS_DATABASE_URL`：元数据库连接地址。生产环境应使用持久化的 MySQL 或
  PostgreSQL。
- `PAS_ENCRYPTION_KEY`：根加密密钥，通过 Secret 环境变量或
  `file:/absolute/restricted/path` 提供。

`PAS_ENCRYPTION_KEY` 必须是 Base64，解码后正好为 32 字节。所有副本必须使用
同一个元数据库和根密钥。请分别备份数据库和根密钥；根密钥丢失后，加密配置、
凭证和共享 JWT 签名密钥均无法恢复。

数据库连接池和监听地址属于进程常量，不是额外的 setup 配置。

## 准备并启动服务

本地 SQLite 测试可执行：

```bash
uv sync --extra dev

export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="$(
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
)"

uv run pas database migrate
uv run pas database check
uv run pas serve
```

Docker 和 Kubernetes 生产部署应使用持久化 MySQL 或 PostgreSQL 元数据库。
启动或滚动发布应用副本之前，将 `pas database migrate` 作为部署迁移步骤执行。
`pas database check` 是只读操作，用于确认数据库已经位于当前应用要求的唯一
Alembic head。

数据库兼容性由 Alembic revision 决定，而不是应用版本号。`pas serve` 会执行
同样的只读检查；数据库未初始化、版本落后、版本高于应用、存在多个 head 或
无法连接时，服务会在初始化之前拒绝启动，且不会自动执行 DDL。迁移生产
元数据库前必须先备份。

## Bootstrap token 生命周期

元数据库为空时，所有副本都可能尝试初始化，但数据库原子插入只会选出一个
获胜者。获胜副本创建唯一的 bootstrap claim，并仅打印一次明文 token；其他
副本读取共享的初始化状态，不会生成竞争 token。

该 token：

- 有效期为 15 分钟；
- 在数据库中只保存 SHA-256 哈希；
- 连续失败 10 次后拒绝继续验证；
- 签发替代 token 时立即失效；
- 激活 `core_admin` 时在同一事务中被消费。

使用同一个元数据库重启进程不会再次打印 token。服务端无法恢复或显示当前
token 的明文。

## 本地初始化

打开 setup UI，输入后端打印的 token，然后创建首个管理员。管理员密码至少
需要 12 个字符。

UI 会先执行只读 dry run，再通过独立的 **Activate module** 操作保存、验证并
激活已检查的配置。后端状态变为 `READY` 后，使用
**Enter administration console** 进入 `/dashboard`。
运行时服务已经在 setup 访问策略后方启动，因此单实例和多副本部署完成该状态
切换后都不需要重启。

只能使用终端的环境可执行：

```bash
pas config init
```

交互命令会无回显读取 bootstrap token。自动化必须从
`PAS_BOOTSTRAP_TOKEN`、`--bootstrap-token-stdin` 或
`--bootstrap-token-file /absolute/restricted/path` 中选择且只能选择一种。
不要把 token 放入 URL、YAML 声明或普通命令行参数。

## Docker

首次启动时，自动生成的 token 会进入容器 stdout，可通过以下命令读取：

```bash
docker logs <container-name>
```

初始化期间，容器日志包含等同于密码的秘密，必须限制日志访问。如果日志不可用
或 token 已过期，应挂载权限受限的可写目录并重新签发：

```bash
docker exec <container-name> \
  pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token

docker exec <container-name> \
  cat /var/run/pas/bootstrap-token
```

将显示的值输入 setup UI，或在同一容器中通过
`--bootstrap-token-file` 执行 `pas config init`。完成接管后删除该文件。

## Kubernetes 多副本

所有 Pod 必须共享 `PAS_DATABASE_URL`，并从 Kubernetes Secret 获得同一个
`PAS_ENCRYPTION_KEY`。只有赢得数据库初始化竞争的 Pod 会打印自动 token。
使用带来源前缀的全 Pod 日志找到该 token：

所有应用 Pod 还必须对每个已注册 MySQL Endpoint 具有一致的 DNS、路由、
安全组和出口访问能力。实例和凭证连接测试由处理 API 请求的 PAS Pod 发起，
与 SQL over HTTP 路径一致，但一次测试只能证明该副本的连通性。

```bash
kubectl logs -n <namespace> deployment/pas \
  --all-pods=true \
  --prefix \
  --since=30m
```

如果 token 不可用或已经过期，选择一个明确的 Pod，并让所有文件操作都在该
Pod 中执行：

```bash
kubectl get pods -n <namespace> \
  -l app.kubernetes.io/name=pas

POD=<pod-name>

kubectl exec -n <namespace> "$POD" -c pas -- \
  pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token

kubectl exec -n <namespace> "$POD" -c pas -- \
  cat /var/run/pas/bootstrap-token
```

替代 claim 保存在共享元数据库中，因此 setup 请求可以到达任意健康副本。
token 文件只存在于当前 Pod；不要连续使用两条
`kubectl exec deployment/pas`，因为滚动发布或 Pod 替换可能选中不同 Pod。
建议将 `/var/run/pas` 挂载为权限受限的 `emptyDir` 或等效临时卷。

纯终端初始化继续使用选定的同一个 Pod：

```bash
kubectl exec -n <namespace> -it "$POD" -c pas -- \
  pas config \
  --bootstrap-token-file /var/run/pas/bootstrap-token \
  init
```

UI 或 CLI 消费 claim 后，删除 Pod 本地文件：

```bash
kubectl exec -n <namespace> "$POD" -c pas -- \
  rm /var/run/pas/bootstrap-token
```

受支持的 Helm Chart 会在 NOTES 中输出同样的文件复制流程。迁移 Hook、多副本
就绪与升级方式参见
[Kubernetes 部署指南](../deployment/kubernetes-helm.md)。

## 恢复

自动 token 未捕获、已经过期、达到失败次数上限，或随 Pod 替换而丢失时，使用
`pas config bootstrap-token issue --output <absolute-path>`。签发替代 token
会原子失效旧 claim，并以 `0600` 模式写入新文件。

输出路径必须是绝对路径且不能已经存在。命令拒绝符号链接，也不会把 token
打印到 stdout。setup 已经完成后，应使用管理员账户认证；bootstrap token
不是持续使用的管理凭证。

## 安全检查

- 按最小权限限制 Docker 日志、Kubernetes 日志、`exec` 和 Secret 访问。
- 不要将 bootstrap token 发送到集中日志、Shell 历史、清单、工单或源码仓库。
- 只在短暂 setup 窗口使用 `0600` 临时文件，消费后立即删除。
- 不要将 `PAS_ENCRYPTION_KEY` 与元数据库备份存放在一起。
- 未消费的 token 一旦暴露，应立即重新签发使其失效。

完成首次接管后，继续阅读
[引导式模块化配置](../configuration/guided-configuration.md)。
