# 升级与回滚

[English](../../en/deployment/upgrade-and-rollback.md)

升级应视为“先迁移数据库，再滚动应用”。迁移成功前不要启动新版本应用 Pod。

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
PAS_VERSION=0.0.7
helm upgrade pas "./polardb-agentic-server-${PAS_VERSION}-chart.tgz" \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:DIGEST \
  --wait --timeout 10m
```

验证迁移 Job、rollout 状态、`/readyz`、配置收敛和认证后的冒烟测试。

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
