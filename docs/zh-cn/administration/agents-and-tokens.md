# Agent 与 Token

[English](../../en/administration/agents-and-tokens.md)

Agent 是非人类 MCP 身份，拥有独立的状态、Token、实例直连绑定、供应绑定和
自有资源。

## 创建与连接

创建 Agent 时填写清晰的名称和用途。详情页展示有效 Token、MCP 服务 URL 和
JSON 客户端配置，其中 MCP Server 名称默认使用 Agent 名称。只复制到预期的
客户端。

管理员视图会直接展示有效 Token，便于运维配置。应把该页面访问视为密钥访问，
不要把 Token 截入截图、工单或日志。

## Token 生命周期

重新生成会立即使旧 Token 失效；吊销会阻止认证，直到签发新 Token。停用
Agent 会独立于 Token 状态阻止新操作。已有 MCP Session 可能保留旧工具目录，
因此状态、Token 或绑定变化后应重新连接。

## 访问绑定

直连绑定选择已注册实例、凭证、权限和能力。SQL 代理能力是可选项，可以开放
`sql:read`，并在 `readwrite` 下开放 `sql:write`。供应绑定只适用于健康的
`multitenant` 后端，可以在没有直连 SQL 权限时单独开放
`db_instance:create`。

已经绑定到 Agent 的实例不会再次出现在新绑定选择器中。需要调整时应删除或
编辑已有绑定，而不是创建重复绑定。

## 复查

定期复查闲置 Agent、最后使用时间、自有资源和 Audit Logs。停用客户端或人员
自动化前先吊销 Token。
