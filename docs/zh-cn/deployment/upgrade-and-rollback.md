# 升级与回滚

[English](../../en/deployment/upgrade-and-rollback.md)

任何目标版本 Pod 接收流量前，都必须完成目标 Schema 验证。Compose 和 Helm
在替换应用前执行迁移；托管滚动升级可以先启动一个保持摘流的目标版本 Pod，
执行确定性的 one-shot 迁移 Pod，验证 Schema 与健康状态后，再将该 Pod 放回流量。

## 升级前

1. 阅读目标 Release notes 和已知问题。
2. 备份元数据库，并验证备份可以恢复。
3. 单独备份完全相同的 `PAS_ENCRYPTION_KEY`。
4. 记录当前镜像 digest、Chart values、数据库 revision 和配置版本。
5. 验证新版本校验和、attestation、SBOM 和镜像 digest。
6. 启动应用副本前，使用候选二进制、原元数据库和原根密钥执行
   `pas database check`。

密钥无法解密存量配置时，检查会以 `DATABASE_ENCRYPTION_KEY_MISMATCH` fail
closed。不要生成替代密钥或删除数据库来强行继续升级；必须先恢复匹配的恢复集。

## Schema 兼容与恢复

`pas database inspect --format json` 只读输出当前 revision、待执行 revision、
发布分类及兼容性，不修改结构或配置。`pas database check --format json` 还会
验证根密钥与存储配置。不带这些选项的原有命令继续可用。

第一代兼容过渡机制在 revision `fb1c2d3e4f5a` 引入。显式迁移在现有
`system_config` 行中发布兼容记录；启动只读该记录并验证所需物理列。没有受支持
契约的未知 revision 仍被拒绝。只有源版本已理解已发布的 bridge 契约，并且
源到目标路径经过验证时，源版本才能在后续扩展后继续运行。

迁移命令对同一元数据库串行执行并保留执行记录。`DATABASE_MIGRATION_BUSY`
表示其他执行者持有锁；`DATABASE_MIGRATION_REPAIR_REQUIRED` 表示中断操作
留下了无法识别的物理状态，须保留证据并修复后再重试。新库在 `system_config`
创建前失败时，使用迁移执行器日志诊断。回滚扩展后的数据库不使用自动 downgrade。

托管模式下，目标进程等待未初始化或落后的 schema 时提供 `/livez`，而
`/readyz` 和业务请求返回 503。它每两秒重新检查兼容性，最多等待 30 分钟；外部
迁移成功后启动业务服务，启动过程本身不执行 DDL。其他 schema 错误继续阻断，
供运维诊断。


镜像内置 manifest 是权威来源，不提供用户覆盖的 `--manifest` 参数。
`pas database migrate --operation-id <stable-id>` 可以将外部操作关联到同一个
数据库/manifest 检查点。执行器只接受 head、序号与内置 Alembic 历史一致的
`NONE` 或增量 `EXPAND` manifest；`CONTRACT` 和 `BREAKING` 发布必须使用独立
维护流程。

### v0.0.12 Schema 边界

v0.0.12 将元数据 revision 从 `fb1c2d3e4f5a` 推进到
`9c1d2e3f4a5b`。该版本分类为 `EXPAND`：只新增知识范围表、带稳定默认值的列和
非唯一索引，不删除或收紧已有对象。

公开 v0.0.11 早于第一代 bridge 契约。因此，从公开 v0.0.11 升级属于维护式升级，
不能作为新旧版本混跑的滚动升级：

1. 停止应用副本和元数据写入。
2. 备份元数据库及其匹配的 `PAS_ENCRYPTION_KEY`。
3. 使用 v0.0.12 镜像执行 `migrate -> check -> migrate -> check`。
4. 只启动 v0.0.12 副本，并完成认证后的冒烟测试。

迁移后不要只把应用镜像回滚到公开 v0.0.11；其 exact-head 检查会拒绝
revision `9c1d2e3f4a5b`。应发布前向修复；或者停止写入，恢复匹配的升级前数据库、
加密密钥、镜像 digest 和部署 values。

经过认证的管理监听器提供 `GET /api/internal/v1/schema`，参数为 `instance_id`
和 `generation`。严格验收的管控检查不可变 manifest 摘要、schema/配置兼容性、
运行时 `READY` 和 `business_smoke: PASSED`。业务检查在当前副本内执行经过认证的
只读 Agent API 请求，不返回临时凭据。原 `/api/internal/v1/status` 契约保持不变。
即使共享迁移检查点已经成功，也必须逐副本验收。

[知识库启用](../knowledge/activation.md)属于独立的配置生效流程。应先完成支持该功能
的镜像升级，再改变开关；旧镜像不执行新的知识准入规则。

## Compose

将 `PAS_IMAGE` 设为新的不可变 digest，然后执行：

```bash
docker compose pull
docker compose run --rm migrate database migrate
docker compose run --rm migrate database check
docker compose up -d --no-deps server
curl --fail http://127.0.0.1:18760/readyz
```

迁移失败时不要更新 server。

从 v0.0.1 Compose 部署升级时，请从 Compose 环境中删除历史
`PYTHONPATH: /app` workaround。v0.0.3 镜像已经正确安装应用和 `pas`
入口，不需要该覆盖项。

## Helm

Chart 的 `pre-upgrade` 迁移 Job 会阻止失败的 Deployment 更新：

```bash
PAS_VERSION=X.Y.Z
helm upgrade pas "./polardb-agentic-server-${PAS_VERSION}-chart.tgz" \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:DIGEST \
  --wait --timeout 10m
```

验证迁移 Job、rollout 状态、`/readyz`、配置收敛和认证后的冒烟测试。

## 托管滚动升级

按副本串行执行：

1. 选中一个 Pod，并从 RS 流量中摘除。
2. 将该 Pod 切换到目标镜像。
3. 在 Pod 保持摘流时，执行或重放该逻辑 PAS 实例的确定性 one-shot Schema
   迁移 Pod。
4. 验证物理 Schema 与目标镜像健康状态。
5. 将 Pod 放回 RS 流量，再继续下一个副本。

托管执行器使用目标 PAS 镜像创建 `restartPolicy: Never` 的 CoreV1 Pod。它读取
与应用相同的 Secret 和运行配置，但拥有独立的生命周期和退出状态。Provider 在
该步骤只需要 Pod 的 `create` 和 `get` 权限，不需要 Batch Job RBAC，也不需要
对应用 Pod 的 `pods/exec` 权限。

Schema 健康以迁移 Pod 成功执行 `migrate -> check -> migrate -> check` 并正常
退出为准。第二次迁移用于证明重放幂等；每次 `pas database check` 都会只读验证
Alembic revision、已识别的物理 Schema、根密钥解密能力和存储配置兼容性。

服务健康使用独立门禁。PAS 会在路由和 Worker 启动前执行同一套只读数据库兼容性
检查。之后 Kubernetes 使用 `/livez` 判断进程存活，使用 `/readyz` 判断元数据库
和已加载配置是否就绪。恢复 RS 流量前，托管 Provider 还会验证目标镜像、Pod 和
容器的 Running/Ready 状态、ENI 注入完成状态以及预期 Pod IP。

第一个副本执行实际变更，后续副本重放同一个已完成迁移标识并验证结果。迁移
命令和任务流步骤必须幂等，临时任务重试不能产生第二个独立变更。

迁移或验证失败时，任务流应中断并保持当前 Pod 摘流。保留失败迁移 Pod、日志、
Revision 和物理 Schema 证据，供人工介入；不要恢复该 Pod 流量，也不要继续
升级剩余副本。旧副本可以继续服务的前提是托管滚动升级只允许 `NONE` 或已验证
的 `EXPAND` 迁移；破坏性的 `CONTRACT` 或 `BREAKING` DDL 必须使用独立维护流程。

运维人员可以先检查失败证据，再显式触发重试：

```bash
kubectl -n <namespace> logs <migration-pod> -c db-migrate
kubectl -n <namespace> delete pod <migration-pod>
```

只有在保留失败证据并修复根因后才能删除该 Pod。重放同一个任务流步骤会使用同一
确定性迁移标识重新创建 Pod。

## 已知的同 Revision Schema 修复

部分托管预发布数据库虽然报告 Alembic revision `f6a7b8c9d0e1`，但缺少后来
加入该 revision 祖先链的 Schema 对象。发布版本的迁移命令只识别完整的旧版
指纹，补跑被跳过的迁移，并将 revision 保持为 `f6a7b8c9d0e1`，因此不会改变
已经记录的回滚边界。

应保留迁移执行器日志。`LEGACY_F6_SCHEMA_UNKNOWN` 表示数据库不符合受支持的
旧版指纹；`LEGACY_F6_SCHEMA_PARTIAL` 表示部分修复制品已经存在，但该步骤并
不完整。两种情况都会 fail closed，并要求检查 Schema；不要 stamp 数据库，也
不要手工创建单个表或列。

应用启动和 `pas database check` 会在受支持的修复前返回
`DATABASE_SCHEMA_REPAIR_REQUIRED`，在修复只完成一部分时返回
`DATABASE_SCHEMA_REPAIR_PARTIAL`，物理 Schema 不符合任一受支持状态时返回
`DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN`。这些检查只读。应使用目标镜像
执行迁移，并在摘流的目标 Pod 恢复流量前再次执行迁移以验证幂等性。

## Aliyun Access version 2 发布

`aliyun_access` 凭证文档使用 version 2。新二进制同时读取此前的直接配置和
version 2，但旧二进制无法安全读取 version 2。首次保存 Aliyun Access 时会原子地
转换完整模块文档；仅启动服务不会重写它。

写入暂停是 operator-enforced 流程，而不是服务端维护模式功能。滚动发布前，应限制
管理员访问，或以其他方式暂停所有 Aliyun Access configuration writes。将新二进制
部署到每个 PAS 副本后，再通过 `/readyz` 验证每个副本都已加载目标配置版本，之后
才允许第一次保存或切换模式。发生 version 2 写入后，不要保留新旧副本混跑。

升级前，应将元数据库中的 `system_config` 与完全一致的 `PAS_ENCRYPTION_KEY`
（configuration master key）成对备份。没有匹配密钥的数据库备份无法解密恢复后的
凭证。保留记录的直接配置和镜像 digest，作为恢复选项。

## 全局审计配置 version 2 发布

SQL 安全策略和可观测性文档共同组成一对全局审计配置。新二进制会将旧的 version 1
配置对只读投影到 version 2 运行时模型；服务启动、`pas database check`、配置读取和
运行时轮询都不会重写数据库。因此，应用滚动发布期间，旧副本仍可继续读取 version 1。

滚动发布前，应暂停 SQL 安全策略和可观测性配置写入。把新二进制部署到所有 PAS
副本，并逐一验证 `/readyz` 和配置收敛后，再恢复写入。处理首次针对任一模块的状态
变更请求前，PAS 会先原子地把两个文档转换为 version 2；revision 已过期的请求会在
转换前被拒绝。转换后不要再运行旧副本，因为旧二进制无法读取 version 2。

首次 version 2 写入前支持只回滚镜像。写入发生后，应保留新二进制并发布前向修复；
或者在启动旧二进制前，同时恢复匹配的升级前 `system_config` 备份和
`PAS_ENCRYPTION_KEY`。Helm 回滚不会恢复这些配置文档。

## 自动供给池发布

在 `dedicated_pool_enabled=false` 时应用增量 Schema。先验证已有 multitenant
创建/查询/删除行为。然后把新二进制部署到每个副本，激活有界购买/成员/Agent
预算，启用 Flag，重启副本，并先用一个内部 `destroy` 自动供给池完成验证，再允许 Pilot
Agent 绑定。随后启用管理员恢复，最后启用 `sanitize_and_reuse`。

PAS 在配置、路由或 Worker 激活前检查 Alembic Head。旧二进制遇到未知的新
Revision 时会以 `DATABASE_SCHEMA_TOO_NEW` 拒绝启动。创建 Dedicated 记录前，
每个副本都必须运行新二进制；新旧版本混合处理不安全。

## 回滚限制

Alembic 迁移是前向操作，受支持的发布流程不会自动降级元数据库 Schema。只有
旧版本明确支持迁移后的 Schema 时，才可以只回滚镜像。否则应停止写入，恢复
升级前的数据库备份、相同的根加密密钥，并重新部署记录的旧镜像 digest 与
values。Helm revision 回滚不会恢复数据库。

首个 Dedicated 资源创建前，可以禁用 Flag；只有旧二进制明确支持迁移后 Schema
时才能只回滚应用。Dedicated 资源或成员已经存在后，应禁用入口和 Worker，但保留
新二进制/数据模型，然后发布前向修复。不要让旧二进制处理 Dedicated 状态：它可能
重复分配、泄露凭证或删除错误的物理集群。数据库回滚必须使用匹配的升级前元数据
备份和 `PAS_ENCRYPTION_KEY`。

对 Aliyun Access 而言，首次 version 2 写入前可以直接回滚。首次 version 2 写入后，
包括尚未激活的草稿保存，旧二进制都无法安全地针对该数据库运行。应先使用新二进制
切回兼容的 `direct_ak` direct mode，再启动旧二进制；或者同时恢复升级前数据库备份
及其匹配的 configuration master key。使用 AssumeRole 或 ECS RAM 角色的配置，在旧
二进制运行前必须切回 direct mode，或从这对匹配的备份恢复。
