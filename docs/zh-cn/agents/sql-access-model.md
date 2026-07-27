# Agent SQL 访问模型

[English](../../en/agents/sql-access-model.md)

Agent SQL-over-HTTP 按实例直连绑定选择性启用。仅选择 `readwrite` 权限不会在
SQL 代理能力关闭时开放 SQL 工具。

## 能力派生

启用 SQL 代理后：

- `readonly` 派生 `sql:read`。
- `readwrite` 派生 `sql:read` 和 `sql:write`。

元数据能力 `db_instance:list`、`db_instance:describe` 和
`db_instance:credentials:read` 相互独立。凭证读取能力通过可审计的 describe
流程显示连接材料；PAS 使用已存凭证代理 SQL 时不要求该能力。

## 资源自洽

Agent 只能在 `list_db_instances` 返回的有效绑定实例或自己的就绪供应资源上
运行 SQL。必填的 `instance_id` 防止 LLM 回退到无关默认实例。供应资源使用
自动创建的凭证，生命周期达到 `READY` 后才能查询。

## SQL 策略

PAS 对每条语句分类，取能力与绑定权限交集，应用行数和超时限制，检查破坏性
确认并记录审计结果。所选 MySQL 账号会独立限制数据库、表和语句，PAS 无法
绕过这些授权。

包含 DDL 的事务遵循 MySQL 隐式提交语义，不能保证每条语句都可回滚。默认
安全策略会阻止 `DROP DATABASE`。

## 运维建议

授予数据库范围的 MySQL 账号，从后端测试凭证，只启用必要能力，然后重新连接
Agent 客户端。授权失败时应重新列出可见资源，而不是修改标识进行猜测。
