# REST API 参考

[English](../../en/reference/rest-api.md)

Web 控制台使用 `/api` 下经过认证的 REST API。MCP 客户端应使用 Streamable
HTTP 端点 `/mcp`；`/mcp/rest` 是旧的人类用户 SQL 接口，不是 Agent 供应
API。

## 认证与安全

人类管理请求使用管理员 Session Cookie 和 `X-PAS-CSRF: 1`，或受支持的
管理员 Bearer Token。Agent Token 不能调用管理员 API。setup 期间，
`POST /api/config` 只接受有效的 `Authorization: Bootstrap ...` claim。

响应使用稳定错误码和脱敏消息，不要依赖原始异常文本。配置激活和停用等变更
动作要求幂等和 revision 控制。

## 主要资源

管理路由包括 `/api/users`、`/api/departments`、`/api/instances`、
`/api/agents`、`/api/credentials`、`/api/provisioning-backends`、
`/api/audit-logs`、`/api/quota` 和 `/api/pool`。嵌套 User 和 Agent 路由
管理实例/供应绑定及自有资源。

实例注册提供创建前和已有实例的连接测试端点。凭证创建/更新有独立测试动作。
连接测试从后端 Pod 执行。

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

部署策略允许时，FastAPI 会公开请求/响应 Schema 的 OpenAPI 元数据。应把
实际部署版本的 Schema 作为权威，并使用不可变发布 Tag 对应的文档示例。
