# REST API 参考

[English](../../en/reference/rest-api.md)

Web 控制台使用 `/api` 下经过认证的 REST API。MCP 客户端使用 Streamable
HTTP 端点 `/mcp`。`/mcp/rest` 前缀同时包含旧的人类用户 SQL 路由和 Agent
数据库生命周期路由；每个路由族强制使用各自的 Principal 类型。

## 认证与安全

人类管理请求使用管理员 Session Cookie 和 `X-PAS-CSRF: 1`，或受支持的
管理员 Bearer Token。Agent Token 不能调用管理员 API。setup 期间，
`POST /api/config` 只接受有效的 `Authorization: Bootstrap ...` claim。

响应使用稳定错误码和脱敏消息，不要依赖原始异常文本。配置激活和停用等变更
动作要求幂等和 revision 控制。

## 主要资源

管理路由包括 `/api/users`、`/api/departments`、`/api/instances`、
`/api/agents`、`/api/credentials`、`/api/provisioning-backends`、
`/api/audit-logs`。嵌套 User 和 Agent 路由
管理实例/供应绑定及自有资源。
Dedicated 管理还增加 `/api/dedicated-pools`、`/api/permission-templates`、
`/api/permission-template-revisions/{id}/sync`、`/api/permission-sync-jobs`
和 `/api/db-instance-resources`。

`/api/polarrag` 提供仅管理员可用的 PolarRAG 实例检查、可信 Space 启用与
同步，以及逐用户企业主体映射。密钥字段只写不读。详见
[PolarRAG MCP 与企业身份](../knowledge/polarrag-mcp.md)。

[企业身份源管理员 API](enterprise-identity-sources-api.md)单独列出飞书与
SharePoint 身份源、Space 绑定、用户身份映射和飞书 ACL 成员快照的管理接口、参数与作用。

Agent 范围的 PolarRAG 访问通过
`/api/agents/{agent_id}/polarrag-bindings` 和
`/api/agents/{agent_id}/user-assignments` 管理。已认证用户通过
`/api/me/agent-connections` 查看自己的分配关系，并用嵌套的 `issue`、
`reveal`、`regenerate` 和 `revoke` 操作管理自己的 Token。敏感响应带有
`Cache-Control: no-store`；管理员响应永远不包含用户 Token 明文。

管理员通过
`GET /api/agents/{agent_id}/polarrag-bindings/{binding_id}/public-resources`
列出某个绑定下符合条件的已同步 ACTIVE PUBLIC 资源，并在同一路由使用 `PUT` 更新
范围。请求字段 `public_knowledge_resource_ids` 为 `null` 时表示实例的全部 PUBLIC
资源，为数组时表示指定范围，为空数组时表示不允许任何 PUBLIC 资源。PERSONAL ID
会被拒绝。更新会以 `agent_polarrag_binding.public_scope.update` 写入审计，并在下一次
MCP Tool 调用生效，无需重新签发 Token。

实例注册提供创建前和已有实例的连接测试端点。凭证创建/更新有独立测试动作。
连接测试从后端 Pod 执行。

原资源池 Router 和人类用户配额 Router 已整体退役。人类用户请求不再自动购买
物理集群；未分配实例的 User 会收到 `NO_INSTANCE_ASSIGNED`，并应联系管理员。

## Agent 数据库生命周期

Agent Bearer Token 调用 `POST /mcp/rest/db-instances`、
`GET /mcp/rest/db-instances/{resource_id}` 和
`DELETE /mcp/rest/db-instances/{resource_id}`。人类 JWT 和 Cookie 会被拒绝。
响应使用 `Cache-Control: no-store`；创建和可重试状态会按需提供 `Location` 或
`Retry-After`。

示例、状态语义、幂等、错误和凭证处理参见
[Agent REST 数据库供应](../database-instances/agent-rest-provisioning.md)。

## 引导式配置

`POST /api/config` 使用一个带版本的命令 Envelope：

```json
{
  "protocol_version": 1,
  "action": "describe",
  "module": "runtime_policy"
}
```

动作包括 `describe`、`plan`、`save_draft`、`validate`、`activate`、
`skip`、`reset`、`disable` 和 `export`。副作用必须提供命令契约要求的字段，
包括适用时的 `expected_revision`、验证凭据或幂等键。

## OpenAPI 发现

应用 Schema `/openapi.json` 排除 Agent 生命周期路由。Agent 使用规范的独立
Schema `/mcp/rest/openapi.json`；人类可以查看 `/mcp/rest/docs`。CI 同时验证
两个 Surface，避免一个 Principal 发现或误用另一个契约。应把实际部署版本的
Schema 作为权威，并使用不可变发布 Tag 对应的文档示例。
