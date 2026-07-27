# CLI 参考

[English](../../en/reference/cli.md) | **简体中文**

`pas` 命令用于管理元数据库、启动服务和执行引导式配置。会访问本地服务状态的
命令执行前，必须提供 `PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY`。

## 数据库生命周期

```bash
pas database check
pas database migrate
```

`check` 是只读操作，仅当数据库 revision 与 PAS 内置的唯一 Alembic head
一致时成功。`migrate` 执行 `alembic upgrade head`。启动或升级应用副本前只
执行一次，生产元数据库必须先备份。

数据库检查使用以下稳定错误码：

- `DATABASE_SCHEMA_NOT_INITIALIZED`
- `DATABASE_SCHEMA_OUTDATED`
- `DATABASE_SCHEMA_TOO_NEW`
- `DATABASE_MIGRATION_HEAD_INVALID`
- `DATABASE_UNAVAILABLE`

错误信息不会包含密码或完整数据库 URL。PAS 不会在 `serve` 时自动降级或迁移。

## 启动服务

```bash
pas serve
```

服务会在配置、JWT 密钥和后台 Worker 初始化前执行相同的只读 Schema 检查。
Schema 为空或落后时，必须显式执行 `pas database migrate`。

## 引导式配置

```bash
pas config modules
pas config show <module>
pas config configure [module]
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
pas config export --file current.yaml
```

bootstrap token 来源、Secret 引用、模块状态、校验和激活流程请参见
[引导式模块化配置](../configuration/guided-configuration.md)。

## Bootstrap token 恢复

```bash
pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token
```

在一个明确选定的 Pod 中运行。目标必须是权限受限可写卷上的全新绝对路径
文件。通过密钥安全通道复制并用于 setup，消费后删除两处副本。
