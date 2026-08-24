# 连接 MCP 客户端

[English](../../en/agents/connect-mcp-client.md)

连接客户端前先创建 Agent 并授予访问。Agent 详情页是 MCP URL 和有效 Token
的事实来源。

PolarRAG 还要求管理员先为 Agent 绑定 PolarRAG 实例并明确分配 PAS 用户。
用户登录后，在 **My Instances > MCP connections** 中签发自己的
`pas_user_agent_` Token。

## 复制客户端配置

**Copy JSON configuration** 会生成：

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

Server 名称默认使用 Agent 名称。使用控制台生成的 JSON 时无需手工替换字段。
Token 应放入客户端密钥存储，而不是源代码仓库。

用户专用 PolarRAG 操作复制的 JSON 字段与上例完全相同，只有 Bearer 值改为
`pas_user_agent_` 凭证。builtin 用户只能在密码保护的查看操作后复制已有 Token。
SSO 用户在签发或重新生成后会看到一次性复制窗口，可分别选择**复制 Token**或
**复制 JSON 配置**；关闭窗口即丢弃明文。重新生成需要确认，并会立即使旧 Token
失效。签发或重新生成时设置可选 `expires_at` 不会给客户端 JSON 增加字段。

私网 HTTP 只适用于隔离的开发环境；生产环境和不可信网络必须使用 HTTPS。

支持 OAuth 的 MCP 客户端可以使用 PAS 动态客户端注册，而不是手工复制 Agent
Token。回调地址必须精确注册：远程回调使用 HTTPS，只有回环回调可以使用 HTTP。
PAS 会在授权和 token 交换时拒绝不同的 redirect URI。客户端从 OAuth 元数据和
access JWT 获取 PAS issuer，不要把 issuer 增加到客户端 JSON。

## 网络与 TLS

客户端必须能够访问外部 HTTPS URL；PAS Pod 必须能够访问元数据库、已注册的
MySQL 端点和选定的阿里云 OpenAPI 端点。按 MCP 客户端文档配置代理和证书
信任，生产环境不要关闭 TLS 校验。

## 刷新授权

工具可见性根据 Agent 状态和有效绑定计算。授予或删除直连访问、SQL 代理、
供应能力或重新生成 Token 后，应重新连接。变更前建立的连接可能保留旧工具
列表。

## 排查连接失败

确认 URL 以 `/mcp` 结尾、请求头使用 `Bearer`、Agent 和 Token 均有效，并且
系统已经完成初始化。使用脱敏服务日志和 Audit Logs；不要把 Token 粘贴到
公开 Issue。
