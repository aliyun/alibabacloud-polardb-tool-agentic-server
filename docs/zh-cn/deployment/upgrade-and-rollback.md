# 升级与回滚

[English](../../en/deployment/upgrade-and-rollback.md)

升级应视为“先迁移数据库，再滚动应用”。迁移成功前不要启动新版本应用 Pod。

## 升级前

1. 阅读目标 Release notes 和已知问题。
2. 备份元数据库，并验证备份可以恢复。
3. 单独备份完全相同的 `PAS_ENCRYPTION_KEY`。
4. 记录当前镜像 digest、Chart values、数据库 revision 和配置版本。
5. 验证新版本校验和、attestation、SBOM 和镜像 digest。

## Compose

将 `PAS_IMAGE` 设为新的不可变 digest，然后执行：

```bash
docker compose pull
docker compose run --rm migrate database migrate
docker compose up -d --no-deps server
curl --fail http://127.0.0.1:18760/readyz
```

迁移失败时不要更新 server。

## Helm

Chart 的 `pre-upgrade` 迁移 Job 会阻止失败的 Deployment 更新：

```bash
helm upgrade pas ./polardb-agentic-server-0.0.2-chart.tgz \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:DIGEST \
  --wait --timeout 10m
```

验证迁移 Job、rollout 状态、`/readyz`、配置收敛和认证后的冒烟测试。

## 回滚限制

Alembic 迁移是前向操作，受支持的发布流程不会自动降级元数据库 Schema。只有
旧版本明确支持迁移后的 Schema 时，才可以只回滚镜像。否则应停止写入，恢复
升级前的数据库备份、相同的根加密密钥，并重新部署记录的旧镜像 digest 与
values。Helm revision 回滚不会恢复数据库。
