# 本地数据库与可选验收回归

[English](local-regression.md)

此开发工具无需 Docker engine，即可执行真实数据库迁移与并发测试。请先将
MySQL 服务端/客户端及 PostgreSQL 可执行文件放入 `PATH`：`mysqld`、`mysql`、
`initdb`、`pg_ctl`、`psql`。使用普通用户运行，不能以 root 启动这些数据库。

从仓库根目录执行：

```bash
uv sync --locked --extra dev
uv run python scripts/dev/run_local_regression.py --output-dir /tmp/pas-native-regression
```

工具在随机回环端口启动私有 MySQL/PostgreSQL 进程，初始化独立的临时数据目录，
并创建名称为 `pas_local_reg_<32 hex digits>` 的随机数据库，不接受已有数据库
URL。继承的 PAS、云凭据和代理设置会被清除。MySQL 的无密码 root 和 PostgreSQL
的 trust 认证仅用于这些一次性本地服务，应在可信开发机器上使用。

矩阵迁移并检查 SQLite、MySQL、PostgreSQL，然后验证目录清理、配置/知识持久化、
MySQL 账户写入、PostgreSQL 凭据/会话竞态及资源容量预留行锁。原生 MySQL CLI
配套用例通过真实连接验证环境文件生成、密码往返、文件权限及无密码回显，
不能替代打包容器镜像验收。
外部数据库的建表 fixture 会先复制模型 metadata，保留源 ORM 约束，避免影响
同一进程后续的 SQLite 测试。

如需同时在全新私有数据库上执行完整后端测试：

```bash
uv run python scripts/dev/run_local_regression.py --full --output-dir /tmp/pas-native-full
```

每次运行记录命令日志、数据库版本、退出码和清理状态。`--full` 还生成
`backend-full.xml`，并在 `local-regression.json` 记录剩余跳过项。命令失败即停止
并保留日志；正常退出仅停止工具自己的数据库进程并删除其数据。强制杀死工具
可能导致清理无法完成。日志和报告应保存在仓库外，不应提交。

## Compose 配置检查

两个 Compose 解析用例需要真实 Docker Compose plugin 或独立 `docker-compose`
可执行文件，无需 Docker engine。使用独立二进制时，先按官方发布的 checksum
校验，再传入绝对可执行路径：

```bash
PAS_TEST_COMPOSE_BINARY=/absolute/path/to/docker-compose \
  uv run python scripts/dev/run_local_regression.py --full --output-dir /tmp/pas-native-full
```

显式指定无效二进制会导致测试失败。未检测到解析器时，这些用例仍会跳过。
不 mock 解析器实现。

## 候选容器与 Kubernetes 验收

使用自己拥有的本地 Docker engine，Kubernetes 检查还需安装 kind 和 Helm。
构建候选镜像但不发布，然后在完整回归工具中显式启用集成用例：

```bash
docker build -t pas-local:acceptance .
uv run python scripts/dev/run_local_regression.py --full \
  --candidate-image pas-local:acceptance \
  --kubernetes-mysql-image mysql:8.0.44 \
  --output-dir "$HOME/pas-container-acceptance"
```

使用 VM 内的 Docker engine 时，输出目录必须与 VM 共享，因为容器用例会绑定
挂载自己创建的临时输出子目录。用例以只读文件系统和调用方 UID/GID 运行真实
候选镜像，连接私有 MySQL 容器，验证密码往返、`0600` 权限及终端无密码回显。
容器和网络采用随机名称，退出时清理。单独运行该用例时，可以用显式
`PAS_TEST_MYSQL_IMAGE` 覆盖默认固定版本的 MySQL 镜像。

仅指定 `--candidate-image` 时启用环境生成用例。`--kubernetes-mysql-image`
还会启用 kind/Helm 生命周期用例，且必须同时指定候选镜像。
`--kind-image registry.example.com/kindest/node:v1.37.0` 可选择镜像节点仓库，
但必须启用 Kubernetes 验收。这些参数要求 `--full`，不会启用真实云用例，也
不接受已有集群。kind 用例创建私有 kubeconfig 和集群，验证两副本在滚动、迁移
失败和重试各阶段的认证读写，然后清理。详见
[Helm smoke 指南](../../docs/zh-cn/deployment/kubernetes-helm.md#受限网络中的发布烟测)。

每次执行应记录源码 revision、Dockerfile、镜像 ID 和架构。本地 kind 生命周期
通过不能证明生产网络、基础设施 provider 行为或两个版本 schema 之间的兼容性。

## 全停机维护升级演练

使用**全新私有**原生 MySQL，将固定源版本升级到当前 checkout。传入本地 MySQL
安装目录，不接收已有服务 URL 或数据目录：

```bash
uv run python scripts/dev/run_maintenance_rehearsal.py \
  --source-ref "$SOURCE_REF" \
  --mysql-basedir /path/to/mysql-installation \
  --expected-mysql-version 8.0.13 \
  --chrome --output-dir /tmp/pas-maintenance-acceptance
```

macOS 可用 `--mysql-arch x86_64` 为 x86_64 MySQL 二进制选择已安装的 Rosetta。
运行前将 `SOURCE_REF` 设为本地可用且已复核的源提交；工具不提供默认源 revision。
源 schema head 必须为 `2c3d4e5f6a7b`，目标必须是 generation-2 CONTRACT 版本。
这是特定历史 schema 的演练工具，并非通用公开版本升级工具；不能替换成 schema head
不同的发布标签。若无法取得所需历史源码，则不能运行此场景。每次使用新的输出目录。

工具导出源代码并安装其锁定 Python 环境。指定 `--chrome` 后还会构建两版前端，
用实际 Google Chrome 在升级、回滚及恢复后检查登录、保留的 Agent 和页面刷新。
它使用随机回环端口及数据库，停止两个真实源进程，在独立库验证配套备份，再迁移、
检查、重放，并检查目标双副本认证。维护隔离期间本地 HTTP 入口返回 503。

环境故障用例包括只读服务、故意错误的密钥，以及缺少后续表 ALTER 权限的本地账号。
部分 DDL 提交后必须拒绝盲目重试，再配套恢复到新数据库并重新升级成功。
仅回滚镜像必须失败；配套回滚须保留源数据且能解密原 token，备份后的写入按预期
消失。没有 mock 应用响应。

退出后仅保留脱敏日志、行哈希、物理 schema 证据、备份哈希、耗时及可选截图。
私有服务、导出的源代码、数据库、恢复密钥和备份均会删除。此工具验收本地元数据路径；
生产摘流隔离、控制面镜像固定、实际 PolarDB 构建和云资源核对仍需运维证据。
详见[升级与回滚](../../docs/zh-cn/deployment/upgrade-and-rollback.md#contract-全停机维护)。

## 本地 MCP 负载场景

原有延迟用例也可在原生 MySQL 和 PostgreSQL 上使用完全独立的本地环境执行：

```bash
uv run python scripts/dev/run_local_mcp_load.py --output-dir /tmp/pas-local-mcp-load
```

工具启动自己的数据库、迁移全新 schema、写入合成后端容量和 Agent token，
并在回环地址启动真实 PAS MCP HTTP listener。每种数据库执行原有的 1000 次
请求、20 并发用例，包含 Server-Timing 断言和删除请求，随后检查全部 1000 行
处于 `DELETING`。正常退出自动清理数据库和 listener；`local-load.json` 记录
完成情况及清理状态。
每种数据库场景使用独立子进程和事件循环，防止已停止 listener 的 SSE 优雅退出
状态影响下一个场景。

仅运行 MCP lifespan，明确不启动资源创建/健康检查 worker 或完整应用 lifespan。
使用浏览器 runner 的 guard 禁止对外联网，不创建物理云资源。测量场景为
`local-loopback-enqueue-only-no-workers`；分位数描述本地请求处理，不设生产
延迟阈值，不能证明云资源创建、部署吞吐或 SLA。

## 旧副本存活的升级例外演练

验证修复镜像的普通 init 自动迁移，可运行 `scripts/dev/run_managed_live_upgrade_rehearsal.py`，
使用下述 MySQL/输出参数，并增加与源 manifest 一致的 `--source-head`
（`9c1d2e3f4a5b`、`cf4a5b6c7d8e` 或 `2c3d4e5f6a7b`）及 `--mode automatic`。
通过 `--source-ref` 显式传入匹配的本地提交；工具不会隐式选择或拉取私有源码。
工具验证混部认证读写、旧镜像重启拒绝、无需 maintenance 的原 init/check，以及 B 串行替换。
`--mode external-manual` 还要求通过 `--legacy-target-ref` 传入本地可用的目标提交，
该提交须包含原维护门禁、预期目标 schema，并使用相同依赖锁。工具加载该服务代码，
保留普通 init 的持续重试，同时执行独立手工迁移；验证 MAINTENANCE_REQUIRED、迁移锁 BUSY
及最终 init 成功，不修改模板。两种模式均不执行已部署 Provider 引擎。
离线工具 `manual_schema_ephemeral.py` 为原 Pod 生成临时容器子资源请求，沿用原凭据引用和
init 实际镜像 digest；须先用 inspect 模式验证运行时支持。

以下仅本地工具在显式要求的紧急迁移例外中保留所选源副本进程，恢复普通 init
迁移/检查后，再串行升级旧副本。工具创建自己的私有 MySQL 服务，不接受客户数据库 URL。

```bash
uv run python scripts/dev/run_live_upgrade_rehearsal.py \
  --source-ref "$SOURCE_REF" \
  --mysql-basedir /path/to/mysql-8.0.13 \
  --mysql-arch x86_64 --expected-mysql-version 8.0.13 \
  --chrome --output-dir /tmp/pas-live-upgrade-evidence
```

仅在主机需要显式指定二进制执行架构时使用 `--mysql-arch`。`SOURCE_REF` 必须在本地
可解析，且 schema head 为 `2c3d4e5f6a7b`。工具记录解析后的源 revision，依赖单独冻结。
`rehearsal.json` 记录 B 进程存活、请求观测、DDL 锁和 liveness、
在线备份恢复及解密、普通门禁拒绝、原 init 重放、混部/目标版认证读写、Chrome 和清理。
这验证约定的进程存活目标，不证明请求零失败、旧镜像重启/回滚或已部署 Provider
任务引擎。混部期间须冻结新的 provider 写入语义。离线工具 `live_upgrade_patch.py`
只生成带身份检查的 patch/delete 文档，不操作集群。

## 外部验收边界

以下验收仍需显式提供对应环境：

| 用例 | 所需环境 | 本地配套覆盖及限制 |
| --- | --- | --- |
| 真实 OpenAPI 凭据 | 已授权云账号及可达端点 | `tests/test_aliyun_openapi_wire.py` 使用真实 SDK 和回环成功/空列表/错误响应；未验证 IAM 及云端路由 |
| 候选镜像环境文件生成 | Docker engine 和候选镜像 | `--candidate-image` 运行真实镜像；仅执行原生 CLI 配套用例不能证明容器打包 |
| Kubernetes 发布生命周期 | Docker、kind、kubectl 和 Helm | `--kubernetes-mysql-image` 执行真实本地集群生命周期；不能证明生产基础设施或跨版本 schema 兼容性 |
| 部署后的 CreateDBInstance 延迟 | 显式 MCP URL/token、MySQL/PostgreSQL 元数据库及明确的性能环境 | 本地负载工具在两种数据库上运行原用例；本地测量不能证明部署后的延迟 |

仅对自己拥有的一次性环境启用这些用例。Mock 或静态契约通过不能视为真实服务、
容器、集群或生产性能验收。浏览器覆盖见
[Playwright 指南](../../docs/zh-cn/operations/browser-e2e.md)。
