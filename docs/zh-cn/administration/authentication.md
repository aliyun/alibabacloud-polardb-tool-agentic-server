# 认证

[English](../../en/administration/authentication.md)

PAS 将首次接管、人类认证和 Agent Token 分离。一种身份类型的凭证不能代替
另一种身份类型。

## 首次接管与内置登录

一次性 bootstrap token 只在系统处于 setup 模式时接受，并在
`core_admin` 激活时被消费。首个管理员随后使用内置登录。Session Cookie
要求 Web 控制台使用的 CSRF 请求头；API 客户端应使用受支持的 Bearer 流程。

密码修改或重置会按照有效的 token-security 策略使相关 Session 失效。不要把
bootstrap token 或密码放入 URL 或配置仓库。

## 可选 SSO

`user_sso` 模块可以保持 `SKIPPED`。启用时配置 HTTPS 外部基础 URL、OIDC
提供方元数据、Client ID、加密 Client Secret、Scope、Claim 和重定向行为。
向用户开放前，应在生产 Ingress 环境验证浏览器重定向和退出。

## Agent 认证

每个 Agent 拥有一个独立管理的 Token。Token 在 `/mcp` 认证 Agent 身份，
不会创建管理员 Session。泄露后应吊销或重新生成，并重新连接 MCP 客户端，
以刷新工具列表和授权快照。

## 失败处理

连续认证失败应通过脱敏的应用日志和审计日志排查。不要要求用户在公开 Issue
中粘贴 Token、Cookie、OIDC Secret 或数据库 URL。
