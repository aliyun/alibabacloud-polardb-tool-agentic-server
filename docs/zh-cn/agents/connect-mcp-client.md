# 连接 MCP 客户端

[English](../../en/agents/connect-mcp-client.md)

连接客户端前先创建 Agent 并授予访问。Agent 详情页是 MCP URL 和有效 Token
的事实来源。

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
