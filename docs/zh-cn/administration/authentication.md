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

## Web 控制台语言

Web 控制台支持英文（`en-US`）和简体中文（`zh-CN`）。首次访问时会跟随浏览器
语言；浏览器语言不受支持时回退到英文。可在登录、初始设置或已登录的控制台页面
使用语言切换器覆盖自动选择。手动选择会保存在浏览器中，并在后续访问时优先生效。

切换显示语言只影响前端标签、提示消息、Ant Design 组件以及本地化的日期或数字
格式。API 请求与响应、标识符、SQL 和后端诊断详情不会被翻译。

## 可选 SSO

`user_sso` 模块可以保持 `SKIPPED`。启用时配置 HTTPS 外部基础 URL、OIDC
提供方元数据、Client ID、加密 Client Secret、Scope、Claim 和重定向行为。
向用户开放前，应在生产 Ingress 环境验证浏览器重定向和退出。
Discovery 元数据必须包含预期 issuer。使用手工端点配置时，必须显式设置
`issuer`、`authorization_endpoint` 和 `token_endpoint`。

对于 MCP OAuth，PAS 会把每次 OIDC 回调绑定到短期、一次性的 state、OIDC
nonce，并在提供方支持时使用 PKCE。失败或重放的回调不能再次完成 MCP 授权。
PAS 会校验 ID Token 的签名、issuer、audience 和 nonce；同时读取 UserInfo 时，
其 `sub` 必须与已验证 ID Token 的 `sub` 完全一致。验证失败返回通用、不可缓存的
认证错误，并消费一次性 state。

## MCP OAuth 安全契约

PAS 向 MCP 客户端开放动态客户端注册，但只接受 authorization code 和 refresh
token grant，response type 只能是 `code`。每个 redirect URI 都会原样保存并精确
比较。远程回调必须使用 HTTPS；HTTP 只允许 `localhost`、`127.0.0.1` 或 `::1`
回环客户端。包含凭证、fragment、通配符、自定义 scheme 或未注册的 redirect
URI 都会被拒绝。

PAS 签发的 MCP access JWT 强制包含 `iss`、`aud`、`sub`、`iat`、`exp`、`jti`
和 `type`。`iss` 必须精确等于 PAS 外部基础 URL，`aud` 必须等于 `/mcp` 资源
URL。每次请求都会校验签名、issuer、audience、过期时间、subject、唯一 token ID
以及 `type=access`。issuer 只存在于 JWT 内，不会增加到复制的 MCP JSON 中。

PAS 会在校验凭证或 grant 前执行 Pod 本地限流：同一账户每分钟最多尝试
builtin 登录 5 次，同一远端地址每分钟最多尝试 builtin 登录 20 次、动态注册
10 次、token 请求 20 次。超过限制时返回 HTTP `429` 和 `Retry-After`。这些是
最小防护，不是分布式集群配额；多副本部署还应在 Ingress 增加统一保护。

## Agent 认证

每个 Agent 拥有一个独立管理的 Token。Token 在 `/mcp` 认证 Agent 身份，
不会创建管理员 Session。泄露后应吊销或重新生成，并重新连接 MCP 客户端，
以刷新工具列表和授权快照。

## 失败处理

连续认证失败应通过脱敏的应用日志和审计日志排查。不要要求用户在公开 Issue
中粘贴 Token、Cookie、OIDC Secret 或数据库 URL。
