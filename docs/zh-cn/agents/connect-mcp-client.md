# 连接 MCP 客户端

[English](../../en/agents/connect-mcp-client.md)

个人连接请打开**连接 MCP**。管理员在**资源**或**访问管理**中授予个人账号数据库访问权限，无需 Agent。知识库使用企业身份、所有权和上游 ACL。

```json
{
  "mcpServers": {
    "pas-personal": {
      "type": "http",
      "url": "https://PAS_HOST/mcp/personal"
    }
  }
}
```

OAuth 客户端必须将该 URL 用作授权 `resource`，刷新保持个人 audience。不支持 OAuth 的客户端可在 **Token 连接**中生成有有效期、仅显示一次的 `pas_personal_` Token。权限、凭据生命周期和回滚参见[账号与资源](../administration/accounts-and-resources.md)。

以下章节描述现有 `/mcp` 连接，保留其 Agent / Workspace 范围，可从**连接 MCP → 已有 Agent 连接与文档上传**进入。个人凭据不能用于 `/mcp`，旧凭据不能用于 `/mcp/personal`。

## 选择用户默认 Agent

PAS 先把登录身份映射为 PAS User，再定位该 User 的 Workspace。Workspace
选择一个活动 Agent，由该 Agent 持有所生效的 MySQL 和 PolarRAG 资源绑定。

- 只有一个可用 Agent 时自动选择。
- 存在多个可用 Agent 时，用户需要在**我的实例**中选择。
- 默认 Agent 后续被停用或授权被撤销时，PAS 会拒绝 Tool 执行，直到用户重新
  选择；不会静默切换资源。
- 登录过程不会自动创建 MySQL 或 PolarRAG 实例。

可用 Agent 包括直接授权，以及通过部门、企业组或已同步身份源组授权的活动
Agent。

## 浏览器 OAuth 流程

PAS 支持 Authorization Code 和 Refresh Token Grant，并强制 PKCE `S256`。
Client 可以使用动态客户端注册或预注册客户端。回调地址必须精确注册：远程
回调使用 HTTPS；回环 HTTP 回调可以使用动态端口。redirect URI、issuer、
resource 或 audience 不匹配时 PAS 会拒绝请求。

最终访问 `/mcp` 的请求仍然包含：

```http
Authorization: Bearer PAS_ACCESS_TOKEN
```

关键边界是该 Token 由 PAS 在验证 SSO 身份后签发。受信任外部 Access Token
必须使用下文明确的交换或直连兼容流程，不会被静默当作浏览器登录凭证。

## 交换外部 Access Token

Client 已经持有受信任的 OIDC、OAuth、飞书或 BUC Access Token 时，推荐使用
OAuth Token Exchange：

```bash
curl --request POST https://PAS_HOST/token \
  --user "${PAS_CLIENT_ID}:${PAS_CLIENT_SECRET}" \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode \
    'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
  --data-urlencode "subject_token=${EXTERNAL_ACCESS_TOKEN}" \
  --data-urlencode \
    'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \
  --data-urlencode 'resource=https://PAS_HOST/mcp' \
  --data-urlencode 'scope=mcp'
```

管理员从**外部应用**页面统一获取 `PAS_CLIENT_ID`、只显示一次的
`PAS_CLIENT_SECRET`、Endpoint、Resource、Scope 和可复制请求。响应只包含 PAS
Access Token，不包含 Refresh Token；访问 `/mcp` 时使用 PAS Access Token。

应用只启用 MCP 目标时，可以省略 `resource` 和 `scope`，PAS 会自动推导；同时
启用 MCP 和 API 时必须传入 `resource`。是否允许传 `agent_id` 由应用选择的
Workspace 默认、固定 Agent 或调用方可选策略决定。
`/api/v1/external-auth/token` 是等价 API 别名。

不要在 `/token` 中省略 `grant_type`。无 Grant Type 的兼容流程不是 Token
Endpoint 扩展，而是可选的 `/mcp` 行为：

```http
Authorization: Bearer EXTERNAL_ACCESS_TOKEN
```

管理员必须显式开启外部 Token MCP 直连。此后 PAS 会在每次请求中到配置的
Provider 验证外部 Token，并且不会签发 PAS Refresh Token。该模式适合存量
Client，但每次 MCP 请求都会受到 Provider 延迟和可用性的影响。

## 静态 Token 兼容模式

机器集成和不支持浏览器 OAuth 的 Client 仍可使用 Agent 详情页的活动
`pas_agent_` Token。用户也可以在**我的实例 > MCP 连接**签发
`pas_user_agent_` Token。复制的配置形态如下：

```json
{
  "mcpServers": {
    "AGENT_NAME": {
      "url": "https://PAS_HOST/mcp",
      "headers": {
        "Authorization": "Bearer AGENT_TOKEN"
      }
    }
  }
}
```

静态 Token 应放入 Client 密钥存储，而不是源代码仓库。builtin 用户需要确认
密码后才能 Reveal 已有 Token；SSO 用户新签发或重新生成的明文只返回一次。
重新生成会立即使旧 Token 失效。

私网 HTTP 只适用于隔离的开发环境；生产环境和不可信网络必须使用 HTTPS。

## 网络与 TLS

客户端必须能够访问外部 HTTPS URL；PAS Pod 必须能够访问元数据库、已注册的
MySQL 端点和选定的阿里云 OpenAPI 端点。按 MCP 客户端文档配置代理和证书
信任，生产环境不要关闭 TLS 校验。

## 刷新授权

工具可见性根据 Workspace 选择的 Agent、Agent 状态和有效绑定计算。修改默认
Agent、资源绑定、SQL 能力或静态 Token 后应重新连接。变更前建立的连接可能
保留旧工具列表。

## 排查连接失败

确认 URL 以 `/mcp` 结尾。浏览器 OAuth 场景应确认 SSO 已激活、浏览器回调
成功且 Workspace 状态为 `ready`。Token Exchange 场景应确认 PAS Client 已
注册、外部 Token 信任已激活，且配置的 Provider 接受该 subject token。外部
Bearer 直连还应确认兼容开关已激活。静态认证场景应确认请求头使用 `Bearer`，
Agent 和 Token 均有效。使用脱敏服务日志和 Audit Logs；不要把 Token 粘贴到
公开 Issue。
