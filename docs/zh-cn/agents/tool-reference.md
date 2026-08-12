# Agent 工具参考

[English](../../en/agents/tool-reference.md)

PAS 动态开放工具。Agent 只能看到有效绑定和资源所有权允许的工具。

## 实例清单与供应

- `list_db_instances(cursor?, limit?, db_type?, source?, status?)` 列出已授权
  绑定实例和自有供应资源。
- `create_db_instance(client_token, db_type, name?)` 在授予
  `db_instance:create` 时创建持久资源。`db_type` 当前为
  `polardb_mysql`；`client_token` 是必填幂等键。
- `describe_db_instance(db_instance_id)` 描述清单工具返回的一个已授权标识。
  只有具备凭证读取授权且资源就绪时才返回凭证。
- `delete_db_instance(db_instance_id)` 只能删除自有供应资源，不能删除已注册
  的物理实例。

分页应使用 `has_more` 和 `next_cursor`。无效 cursor 返回
`INVALID_CURSOR`，不会静默回到第一页。

## SQL 与 Schema

- `run_sql(sql, instance_id, database?, branch?, max_rows?, cursor?, confirm?)`
  执行单条语句。
- `run_sql_transaction(sql_statements, instance_id, database?, confirm?)`
  在一个事务中执行列表，同时遵循 MySQL 隐式提交规则。
- `describe_schema(instance_id, database?, table_pattern?, include_columns?,
  cursor?, max_tables?)` 返回表、注释和可选列信息。

对于 Agent，`instance_id` 必填，且必须与 `list_db_instances` 返回的
`db_instance_id` 完全一致。显示名称和集群 ID 不能代替。`sql:read` 允许只读
操作；`sql:write` 还要求 `readwrite` 绑定和后端权限。

## 面向用户的工具

人类用户 Session 在运行时访问允许时还可能开放 `set_default_instance`、
`list_branches`、`create_branch` 和 `delete_branch`。Agent 不应假定这些工具
一定存在。具有可见 PolarRAG 资源的已认证用户还会获得
`list_knowledge_resources`、`kb_search`、`kb_fetch_context`、
`doc_find_by_name`、`doc_status`、`doc_recall` 和 `doc_get_original`。
机器 `pas_agent_` Token 永远不会获得 PolarRAG Tool。`pas_user_agent_`
Token 只会获得上述 7 个 Tool，同时应用被分配用户的 ACL Context 和 Agent 的
PolarRAG 实例边界。详见
[PolarRAG MCP 与企业身份](../knowledge/polarrag-mcp.md)。

## 可操作错误

工具返回 `INSTANCE_NOT_ACCESSIBLE` 时，先调用 `list_db_instances`，使用其中
一个标识。`DB_INSTANCE_NOT_FOUND` 表示标识未知或已不可见。
`NO_PROVISIONING_BACKEND` 和 `CAPACITY_EXHAUSTED` 需要管理员检查供应绑定或
容量。`DATABASE_REQUIRED` 表示直连绑定没有默认数据库：执行
`SHOW DATABASES`，选择有权访问的数据库，并携带 `database` 参数重试。
`RATE_LIMITED` 应采用有界重试。绝不能猜测其他租户的标识或凭证。
