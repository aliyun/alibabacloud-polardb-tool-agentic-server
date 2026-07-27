# 故障排查

[English](../../en/operations/troubleshooting.md)

从最小失败边界开始，并保留脱敏证据。

## 服务无法启动

运行 `pas database check`。重启前解决
`DATABASE_SCHEMA_NOT_INITIALIZED`、`DATABASE_SCHEMA_OUTDATED`、
`DATABASE_SCHEMA_TOO_NEW`、`DATABASE_MIGRATION_HEAD_INVALID` 或
`DATABASE_UNAVAILABLE`。不要绕过门禁，也不要让每个副本都执行迁移。

解密失败时，确认所有 Pod 使用同一个原始 `PAS_ENCRYPTION_KEY`。不要在生产
数据库上试用替换密钥。

## Pod 未就绪

检查 `/readyz`，比较 `desired_config_version` 和
`loaded_config_version`，再查看 `last_reload_error` 和模块错误。验证数据库
延迟，并确认已经经过轮询间隔。配置落后的 Pod 不接收 Service 流量是正确行为。

## 外部验证失败

DNS、路由、TLS、凭证和权限失败使用不同脱敏代码。VPC 模式下，从后端 Pod
测试地域 `polardb-vpc` 和 `sts-vpc` 端点解析。实例 Test Connection 也从该
Pod 发起；检查 MySQL 白名单、安全组、host、port、用户名和密码。

## MCP 或 SQL 失败

绑定变化后重新连接。调用 `list_db_instances`，把返回的 `db_instance_id`
作为 `instance_id`，并确认绑定开放了所需 SQL 能力。然后验证已存 MySQL
账号具有请求数据库和语句权限。确定拒绝层之前不要扩大权限。

## 供应卡住

检查后端健康、容量、生命周期状态、Worker 所有权和资源失败代码。
`enable_multi_tenant` 必须开启，供应管理员必须通过预检。只通过受支持的恢复
动作重试，以保持幂等和清理状态完整。
